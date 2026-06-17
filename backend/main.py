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
import json
import asyncio
import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

app = FastAPI(title="Chatbot API")

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
DEFAULT_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:3b")

# Number of model layers to offload to GPU. 0 = CPU-only, which is more stable
# on older / low-VRAM GPUs (e.g. GT 740). Increase if the hardware supports it.
OLLAMA_NUM_GPU = int(os.getenv("OLLAMA_NUM_GPU", "0"))


# Incoming chat request body. `messages` follows the OpenAI-style role/content
# format that Ollama accepts directly. `model` defaults to DEFAULT_MODEL but can
# be overridden by the client (e.g. for a model-selector UI).
class ChatRequest(BaseModel):
    messages: list[dict]
    model: str = DEFAULT_MODEL


@app.get("/api/health")
async def health():
    """Liveness/readiness probe used by Kubernetes and the deploy script."""
    return {"status": "ok", "ollama": OLLAMA_BASE_URL}


@app.post("/api/chat")
async def chat(payload: ChatRequest):
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
                async with httpx.AsyncClient(timeout=None) as client:
                    async with client.stream(
                        "POST",
                        f"{OLLAMA_BASE_URL}/api/chat",
                        json={
                            "model": payload.model,
                            "messages": payload.messages,
                            "stream": True,
                            "options": {"num_gpu": OLLAMA_NUM_GPU},
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

                yield f"data: {value}\n\n"

                if done.is_set() and queue.empty():
                    break
        finally:
            if not pump_task.done():
                pump_task.cancel()

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
