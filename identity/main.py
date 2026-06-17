import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from jose import JWTError, jwt
from motor.motor_asyncio import AsyncIOMotorClient
from passlib.context import CryptContext
from pydantic import BaseModel, EmailStr, Field
import strawberry
from strawberry.fastapi import GraphQLRouter

APP_NAME = "Identity API"
ACCESS_TTL_MINUTES = int(os.getenv("ACCESS_TTL_MINUTES", "15"))
REFRESH_TTL_DAYS = int(os.getenv("REFRESH_TTL_DAYS", "30"))
JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")
JWT_SECRET = os.getenv("JWT_SECRET", "CHANGE_ME_IDENTITY_SECRET")
MONGO_URI = os.getenv("MONGO_URI", "mongodb://identity:identity-change-me@mongodb.identity.svc.cluster.local:27017/identity?authSource=admin")
MONGO_DB_NAME = os.getenv("MONGO_DB_NAME", "identity")

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto", bcrypt__rounds=12)

app = FastAPI(title=APP_NAME)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

mongo_client: Optional[AsyncIOMotorClient] = None


class SignupRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    display_name: str = Field(min_length=2, max_length=80)


class SigninRequest(BaseModel):
    email: EmailStr
    password: str


class RefreshRequest(BaseModel):
    refresh_token: str


class SignoutRequest(BaseModel):
    refresh_token: str


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int


class UserOut(BaseModel):
    id: str
    email: EmailStr
    display_name: str
    created_at: datetime


@app.on_event("startup")
async def startup() -> None:
    global mongo_client
    mongo_client = AsyncIOMotorClient(MONGO_URI)
    await mongo_client.admin.command("ping")

    users = db()["users"]
    refresh_tokens = db()["refresh_tokens"]
    await users.create_index("email", unique=True)
    await refresh_tokens.create_index("jti", unique=True)
    await refresh_tokens.create_index("expires_at", expireAfterSeconds=0)


@app.on_event("shutdown")
async def shutdown() -> None:
    if mongo_client is not None:
        mongo_client.close()


def db():
    if mongo_client is None:
        raise RuntimeError("Mongo client not initialized")
    return mongo_client[MONGO_DB_NAME]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


def encode_token(subject: str, token_type: str, ttl_seconds: int, extra: Optional[dict] = None) -> str:
    payload = {
        "sub": subject,
        "type": token_type,
        "iat": int(utcnow().timestamp()),
        "exp": int((utcnow() + timedelta(seconds=ttl_seconds)).timestamp()),
    }
    if extra:
        payload.update(extra)
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except JWTError as exc:
        raise HTTPException(status_code=401, detail="Invalid token") from exc


async def issue_tokens(user_id: str) -> TokenResponse:
    access_ttl = ACCESS_TTL_MINUTES * 60
    refresh_ttl = REFRESH_TTL_DAYS * 24 * 60 * 60
    refresh_jti = str(uuid.uuid4())

    access_token = encode_token(user_id, "access", access_ttl)
    refresh_token = encode_token(user_id, "refresh", refresh_ttl, {"jti": refresh_jti})

    await db()["refresh_tokens"].insert_one(
        {
            "jti": refresh_jti,
            "user_id": user_id,
            "expires_at": utcnow() + timedelta(seconds=refresh_ttl),
            "revoked": False,
            "created_at": utcnow(),
        }
    )

    return TokenResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=access_ttl,
    )


async def get_current_user(authorization: Optional[str] = Header(default=None)) -> dict:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")

    token = authorization.split(" ", 1)[1]
    payload = decode_token(token)

    if payload.get("type") != "access":
        raise HTTPException(status_code=401, detail="Invalid token type")

    user_id = payload.get("sub")
    user = await db()["users"].find_one({"_id": user_id}, {"password_hash": 0})
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    return user


@app.get("/health")
async def health():
    await db().command("ping")
    return {"status": "ok", "service": "identity"}


@app.post("/auth/signup", response_model=TokenResponse)
async def signup(payload: SignupRequest):
    users = db()["users"]
    if await users.find_one({"email": payload.email.lower()}):
        raise HTTPException(status_code=409, detail="Email already registered")

    user_id = str(uuid.uuid4())
    created_at = utcnow()
    await users.insert_one(
        {
            "_id": user_id,
            "email": payload.email.lower(),
            "display_name": payload.display_name,
            "password_hash": hash_password(payload.password),
            "created_at": created_at,
        }
    )

    return await issue_tokens(user_id)


@app.post("/auth/signin", response_model=TokenResponse)
async def signin(payload: SigninRequest):
    user = await db()["users"].find_one({"email": payload.email.lower()})
    if not user or not verify_password(payload.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    return await issue_tokens(user["_id"])


@app.post("/auth/refresh", response_model=TokenResponse)
async def refresh(payload: RefreshRequest):
    token_payload = decode_token(payload.refresh_token)
    if token_payload.get("type") != "refresh":
        raise HTTPException(status_code=401, detail="Invalid token type")

    jti = token_payload.get("jti")
    user_id = token_payload.get("sub")
    if not jti or not user_id:
        raise HTTPException(status_code=401, detail="Malformed refresh token")

    token_doc = await db()["refresh_tokens"].find_one({"jti": jti, "revoked": False})
    if not token_doc:
        raise HTTPException(status_code=401, detail="Refresh token revoked or unknown")

    await db()["refresh_tokens"].update_one({"jti": jti}, {"$set": {"revoked": True}})
    return await issue_tokens(user_id)


@app.post("/auth/signout")
async def signout(payload: SignoutRequest):
    token_payload = decode_token(payload.refresh_token)
    jti = token_payload.get("jti")
    if not jti:
        raise HTTPException(status_code=400, detail="Malformed refresh token")

    await db()["refresh_tokens"].update_one({"jti": jti}, {"$set": {"revoked": True}})
    return {"status": "ok"}


@app.get("/auth/me", response_model=UserOut)
async def me(current_user: dict = Depends(get_current_user)):
    return UserOut(
        id=current_user["_id"],
        email=current_user["email"],
        display_name=current_user["display_name"],
        created_at=current_user["created_at"],
    )


@strawberry.type
class IdentityUser:
    id: str
    email: str
    display_name: str


@strawberry.type
class Query:
    @strawberry.field
    async def me(self, info) -> Optional[IdentityUser]:
        request = info.context["request"]
        auth = request.headers.get("Authorization")
        try:
            user = await get_current_user(auth)
            return IdentityUser(
                id=user["_id"],
                email=user["email"],
                display_name=user["display_name"],
            )
        except HTTPException:
            return None


schema = strawberry.Schema(query=Query)
graphql_app = GraphQLRouter(schema)
app.include_router(graphql_app, prefix="/graphql")
