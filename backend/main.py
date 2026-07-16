"""
Chatbot backend — FastAPI service that proxies chat requests to a local Ollama instance.
Request flow:
  Browser → Nginx (frontend) → /api/* reverse-proxy → this service → Ollama HTTP API
Key design choices:
  - Responses are streamed as Server-Sent Events (SSE) so tokens appear in the UI
    immediately as the model generates them.
  - A background asyncio task reads from Ollama and puts lines onto a queue; the
    SSE generator drains that queue, emitting ": keepalive" comments every 20 s of
    silence so that intermediate proxies (Cloudflare Tunnel, Nginx) do not close
    the connection before the model finishes.
  - num_gpu is configurable via OLLAMA_NUM_GPU (default 0 = CPU-only) to avoid
    instability on older GPU hardware such as the GT 740.
"""
import os
import re
import json
import asyncio
import httpx
from datetime import datetime, timezone
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, field_validator
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from motor.motor_asyncio import AsyncIOMotorClient
from jose import JWTError, jwt
from bson import ObjectId
from bson.errors import InvalidId

limiter = Limiter(key_func=get_remote_address)
app = FastAPI(title="Chatbot API")
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["POST", "GET"],
    allow_headers=["*"],
)

# Base URL of the Ollama server. In Kubernetes this is set to the node's LAN IP
# via the OLLAMA_BASE_URL env var injected by the backend Deployment manifest.
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")

# Ollama model tag to use for every chat request. Overridable per-request.
DEFAULT_MODEL = os.getenv("OLLAMA_MODEL", "phi4-mini")

# Number of model layers to offload to GPU. 0 = CPU-only, which is more stable
# on older / low-VRAM GPUs (e.g. GT 740). Increase if the hardware supports it.
OLLAMA_NUM_GPU = int(os.getenv("OLLAMA_NUM_GPU", "0"))

# Disable model "thinking" traces by default so streamed output appears as
# user-visible content tokens instead of hidden reasoning tokens.
OLLAMA_THINK = os.getenv("OLLAMA_THINK", "false").strip().lower() in {"1", "true", "yes", "on"}

# Keep responses focused while still allowing useful depth.
DEFAULT_SYSTEM_PROMPT = os.getenv(
    "CHATBOT_SYSTEM_PROMPT",
    (
        "You are a helpful assistant. Provide clear, practical answers with enough detail "
        "to be useful. Prefer concise structure (short paragraphs or bullets) instead of "
        "rambling. Do not output internal reasoning or self-talk. If the user asks for "
        "a short or exact reply, comply exactly."
    ),
)

# Balanced decoding defaults for older hardware: richer answers without a big
# latency jump compared with very short outputs.
OLLAMA_TEMPERATURE = float(os.getenv("OLLAMA_TEMPERATURE", "0.35"))
OLLAMA_TOP_P = float(os.getenv("OLLAMA_TOP_P", "0.92"))
OLLAMA_NUM_PREDICT = int(os.getenv("OLLAMA_NUM_PREDICT", "384"))

# MongoDB for chat history. Optional — history is silently disabled when unset.
CHATBOT_MONGO_URI = os.getenv("CHATBOT_MONGO_URI", "")

# JWT verification — must match the identity service so access tokens can be
# decoded here without an extra round-trip.
JWT_SECRET    = os.getenv("JWT_SECRET", "")
JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")
JWT_AUDIENCE  = os.getenv("JWT_AUDIENCE", "global-apps")

# ── MongoDB client (initialised at startup) ───────────────────────────────────
_mongo_client: AsyncIOMotorClient | None = None
_chat_db = None


@app.on_event("startup")
async def _startup() -> None:
    global _mongo_client, _chat_db
    if CHATBOT_MONGO_URI:
        _mongo_client = AsyncIOMotorClient(CHATBOT_MONGO_URI)
        _chat_db = _mongo_client["chatbot"]
        await _chat_db.conversations.create_index(
            [("user_id", 1), ("updated_at", -1)]
        )
        await _chat_db.messages.create_index(
            [("conversation_id", 1), ("created_at", 1)]
        )


async def _get_user_id(request: Request) -> str | None:
    """Decode the JWT in the Authorization header and return the `sub` claim.
    Returns None when the token is absent, malformed, or the secret is unconfigured."""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer ") or not JWT_SECRET:
        return None
    try:
        payload = jwt.decode(
            auth[7:], JWT_SECRET,
            algorithms=[JWT_ALGORITHM],
            audience=JWT_AUDIENCE,
        )
        return payload.get("sub")
    except JWTError:
        return None


# Incoming chat request body. `messages` follows the OpenAI-style role/content
# format that Ollama accepts directly. `model` defaults to DEFAULT_MODEL but can
# be overridden by the client (e.g. for a model-selector UI).

_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
MAX_MESSAGE_LENGTH = 4000
MAX_MESSAGES = 40
ALLOWED_ROLES = {"user", "assistant", "system"}


class ChatRequest(BaseModel):
    messages: list[dict]
    model: str = DEFAULT_MODEL

    @field_validator("messages")
    @classmethod
    def validate_messages(cls, messages: list[dict]) -> list[dict]:
        if not messages:
            raise ValueError("messages must not be empty")
        if len(messages) > MAX_MESSAGES:
            raise ValueError(f"too many messages (max {MAX_MESSAGES})")
        cleaned = []
        for msg in messages:
            role = str(msg.get("role", "")).strip()
            content = str(msg.get("content", ""))
            if role not in ALLOWED_ROLES:
                raise ValueError(f"invalid role '{role}'")
            content = _CONTROL_CHAR_RE.sub("", content)
            if len(content) > MAX_MESSAGE_LENGTH:
                content = content[:MAX_MESSAGE_LENGTH]
            cleaned.append({"role": role, "content": content})
        return cleaned

    conversation_id: str | None = None

    @field_validator("model")
    @classmethod
    def validate_model(cls, v: str) -> str:
        if not re.match(r"^[\w.:\-]{1,100}$", v):
            raise ValueError("invalid model name")
        return v

    @field_validator("conversation_id")
    @classmethod
    def validate_conversation_id(cls, v: str | None) -> str | None:
        if v is None:
            return None
        try:
            ObjectId(v)
            return v
        except (InvalidId, Exception):
            raise ValueError("invalid conversation_id")


class ConversationCreate(BaseModel):
    title: str


class ConversationRename(BaseModel):
    title: str


@app.get("/api/health")
async def health():
    """Liveness/readiness probe used by Kubernetes and the deploy script."""
    return {"status": "ok", "model": DEFAULT_MODEL}


# ── Conversation history endpoints ────────────────────────────────────────────

@app.get("/api/conversations")
async def list_conversations(request: Request):
    user_id = await _get_user_id(request)
    if not user_id or _chat_db is None:
        return []
    cursor = (
        _chat_db.conversations
        .find({"user_id": user_id}, {"_id": 1, "title": 1, "updated_at": 1})
        .sort("updated_at", -1)
        .limit(5)
    )
    result = []
    async for doc in cursor:
        result.append({
            "id": str(doc["_id"]),
            "title": doc["title"],
            "updated_at": doc["updated_at"].isoformat(),
        })
    return result


@app.post("/api/conversations")
async def create_conversation(request: Request, body: ConversationCreate):
    user_id = await _get_user_id(request)
    if not user_id or _chat_db is None:
        raise HTTPException(status_code=403, detail="Authentication required")
    title = body.title.strip()[:80] or "New Chat"
    now = datetime.now(timezone.utc)
    result = await _chat_db.conversations.insert_one({
        "user_id": user_id,
        "title": title,
        "created_at": now,
        "updated_at": now,
    })
    # Keep only the 5 most recent conversations — delete any older ones.
    all_ids = await _chat_db.conversations.find(
        {"user_id": user_id}, {"_id": 1}
    ).sort("updated_at", -1).to_list(None)
    if len(all_ids) > 5:
        old_ids = [doc["_id"] for doc in all_ids[5:]]
        await _chat_db.conversations.delete_many({"_id": {"$in": old_ids}, "user_id": user_id})
        await _chat_db.messages.delete_many({"conversation_id": {"$in": [str(i) for i in old_ids]}})
    return {"id": str(result.inserted_id), "title": title}


@app.delete("/api/conversations/{conv_id}")
async def delete_conversation(conv_id: str, request: Request):
    user_id = await _get_user_id(request)
    if not user_id or _chat_db is None:
        raise HTTPException(status_code=403, detail="Authentication required")
    try:
        oid = ObjectId(conv_id)
    except (InvalidId, Exception):
        raise HTTPException(status_code=400, detail="Invalid conversation id")
    res = await _chat_db.conversations.delete_one({"_id": oid, "user_id": user_id})
    if res.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Not found")
    await _chat_db.messages.delete_many({"conversation_id": conv_id})
    return {"ok": True}


@app.patch("/api/conversations/{conv_id}/title")
async def rename_conversation(conv_id: str, request: Request, body: ConversationRename):
    user_id = await _get_user_id(request)
    if not user_id or _chat_db is None:
        raise HTTPException(status_code=403, detail="Authentication required")
    title = body.title.strip()[:80]
    if not title:
        raise HTTPException(status_code=400, detail="Title required")
    try:
        oid = ObjectId(conv_id)
    except (InvalidId, Exception):
        raise HTTPException(status_code=400, detail="Invalid conversation id")
    res = await _chat_db.conversations.update_one(
        {"_id": oid, "user_id": user_id},
        {"$set": {"title": title, "updated_at": datetime.now(timezone.utc)}},
    )
    if res.matched_count == 0:
        raise HTTPException(status_code=404, detail="Not found")
    return {"ok": True}


@app.get("/api/conversations/{conv_id}/messages")
async def get_conversation_messages(conv_id: str, request: Request):
    user_id = await _get_user_id(request)
    if not user_id or _chat_db is None:
        raise HTTPException(status_code=403, detail="Authentication required")
    try:
        oid = ObjectId(conv_id)
    except (InvalidId, Exception):
        raise HTTPException(status_code=400, detail="Invalid conversation id")
    conv = await _chat_db.conversations.find_one({"_id": oid, "user_id": user_id})
    if not conv:
        raise HTTPException(status_code=404, detail="Not found")
    cursor = _chat_db.messages.find(
        {"conversation_id": conv_id}
    ).sort("created_at", 1)
    msgs = []
    async for doc in cursor:
        msgs.append({"role": doc["role"], "content": doc["content"]})
    return msgs


@app.post("/api/chat")
@limiter.limit("10/minute")
async def chat(request: Request, payload: ChatRequest):
    user_id = await _get_user_id(request)

    """
    Stream a chat completion from Ollama as Server-Sent Events.

    The SSE event format is:
        data: <raw JSON line from Ollama>\n\n

    Each JSON line contains a partial token in `message.content`; the final
    line has `done: true`.  The frontend reads these incrementally and appends
    tokens to the assistant bubble as they arrive.

    Keepalive comments (': keepalive') are injected every 20 s of model
    silence to prevent proxy idle-timeout 524 errors from Cloudflare.
    """
    async def stream_response():
        # Send an early byte so upstream proxies do not time out waiting.
        yield ": stream-open\n\n"

        # queue carries (kind, value) tuples from the Ollama reader task to the
        # SSE generator coroutine.  kind is "line" (a raw JSON token line) or
        # "error" (a plain-text error message).
        queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue()

        # done is set by pump_ollama when the Ollama stream is fully consumed or
        # has errored, signalling the SSE loop to flush remaining queue items
        # and then close the response.
        done = asyncio.Event()

        async def pump_ollama():
            """Read streaming lines from Ollama and forward them onto the queue."""
            try:
                messages = payload.messages
                if DEFAULT_SYSTEM_PROMPT and not any(m.get("role") == "system" for m in messages):
                    messages = [{"role": "system", "content": DEFAULT_SYSTEM_PROMPT}, *messages]

                async with httpx.AsyncClient(timeout=None) as client:
                    async with client.stream(
                        "POST",
                        f"{OLLAMA_BASE_URL}/api/chat",
                        json={
                            "model": payload.model,
                            "messages": messages,
                            "stream": True,
                            "think": OLLAMA_THINK,
                            "options": {
                                "num_gpu": OLLAMA_NUM_GPU,
                                "temperature": OLLAMA_TEMPERATURE,
                                "top_p": OLLAMA_TOP_P,
                                "num_predict": OLLAMA_NUM_PREDICT,
                            },
                        },
                    ) as response:
                        if response.status_code != 200:
                            await queue.put(("error", f"Ollama returned {response.status_code}"))
                            return
                        async for line in response.aiter_lines():
                            if line:
                                await queue.put(("line", line))
            except httpx.ConnectError:
                await queue.put(
                    (
                        "error",
                        (
                            f"Cannot connect to Ollama at {OLLAMA_BASE_URL}. "
                            "Verify OLLAMA_BASE_URL and ensure Ollama is reachable from the backend pod."
                        ),
                    )
                )
            except Exception as exc:
                await queue.put(("error", str(exc)))
            finally:
                done.set()

        # Start the Ollama reader as a concurrent task so the SSE generator
        # can yield keepalives while waiting for the model to produce tokens.
        pump_task = asyncio.create_task(pump_ollama())
        _response_tokens: list[str] = []

        try:
            while True:
                try:
                    kind, value = await asyncio.wait_for(queue.get(), timeout=20)
                except asyncio.TimeoutError:
                    # No token arrived within 20 s. If the pump is done and the
                    # queue is empty the model has finished; otherwise emit a
                    # keepalive comment to hold the connection open.
                    if done.is_set() and queue.empty():
                        break
                    yield ": keepalive\n\n"
                    continue

                if kind == "error":
                    yield f"data: {json.dumps({'error': value})}\n\n"
                    break

                # Accumulate content tokens for history persistence.
                try:
                    tok = json.loads(value).get("message", {}).get("content", "")
                    if tok:
                        _response_tokens.append(tok)
                except Exception:
                    pass

                yield f"data: {value}\n\n"

                if done.is_set() and queue.empty():
                    break
        finally:
            if not pump_task.done():
                pump_task.cancel()

            # Persist messages to MongoDB — best-effort, never blocks the response.
            if (
                user_id
                and _chat_db is not None
                and payload.conversation_id
                and _response_tokens
                and done.is_set()
            ):
                try:
                    full_response = "".join(_response_tokens)
                    now = datetime.now(timezone.utc)
                    user_msgs = [m for m in payload.messages if m["role"] == "user"]
                    if user_msgs:
                        await _chat_db.messages.insert_one({
                            "conversation_id": payload.conversation_id,
                            "role": "user",
                            "content": user_msgs[-1]["content"],
                            "created_at": now,
                        })
                    await _chat_db.messages.insert_one({
                        "conversation_id": payload.conversation_id,
                        "role": "assistant",
                        "content": full_response,
                        "created_at": datetime.now(timezone.utc),
                    })
                    await _chat_db.conversations.update_one(
                        {
                            "_id": ObjectId(payload.conversation_id),
                            "user_id": user_id,
                        },
                        {"$set": {"updated_at": datetime.now(timezone.utc)}},
                    )
                except Exception:
                    pass  # history is best-effort

    return StreamingResponse(
        stream_response(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/models")
async def list_models():
    """
    Return the list of models currently installed in Ollama.

    Proxies GET /api/tags from the Ollama server. The response JSON contains
    a `models` array that the frontend can use to populate a model selector.
    Returns 502 if Ollama is unreachable.
    """
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{OLLAMA_BASE_URL}/api/tags")
            resp.raise_for_status()
            return resp.json()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))