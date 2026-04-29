import base64
import hashlib
import hmac
import json
import os
import secrets
import time

from fastapi import HTTPException, Request
from fastapi.responses import Response
from fastapi.security import APIKeyHeader
from pydantic import BaseModel


class LoginRequest(BaseModel):
    password: str


AUTH_ENABLED = os.getenv("AUTH_ENABLED", "0").lower() in {"1", "true", "yes", "on"}
AUTH_PASSWORD = os.getenv("API_PASSWORD", "admin123")
AUTH_TOKEN_TTL_SECONDS = int(os.getenv("AUTH_TOKEN_TTL_SECONDS", "86400"))
AUTH_SECRET = os.getenv("AUTH_SECRET", secrets.token_hex(32))

api_key_scheme = APIKeyHeader(name="x-api-password", auto_error=False)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("utf-8").rstrip("=")


def _b64url_decode(data: str) -> bytes:
    padding = "=" * ((4 - len(data) % 4) % 4)
    return base64.urlsafe_b64decode(data + padding)


def create_access_token() -> str:
    payload = {
        "exp": int(time.time()) + AUTH_TOKEN_TTL_SECONDS,
        "scope": "api",
    }
    payload_str = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    payload_part = _b64url(payload_str.encode("utf-8"))
    signature = hmac.new(AUTH_SECRET.encode("utf-8"), payload_part.encode("utf-8"), hashlib.sha256).digest()
    signature_part = _b64url(signature)
    return f"{payload_part}.{signature_part}"


def verify_access_token(token: str) -> bool:
    try:
        payload_part, signature_part = token.split(".", 1)
        expected_signature = hmac.new(
            AUTH_SECRET.encode("utf-8"), payload_part.encode("utf-8"), hashlib.sha256
        ).digest()
        provided_signature = _b64url_decode(signature_part)
        if not hmac.compare_digest(expected_signature, provided_signature):
            return False

        payload = json.loads(_b64url_decode(payload_part).decode("utf-8"))
        if int(payload.get("exp", 0)) < int(time.time()):
            return False
        return payload.get("scope") == "api"
    except Exception:
        return False


async def legacy_auth_header_valid(request: Request) -> bool:
    password_header = await api_key_scheme(request)
    return bool(password_header and password_header == AUTH_PASSWORD)


async def auth_middleware(request: Request, call_next):
    if not AUTH_ENABLED:
        return await call_next(request)

    path = request.url.path
    if not path.startswith("/api/"):
        return await call_next(request)

    public_paths = {"/api/health", "/api/auth/login"}
    if path in public_paths:
        return await call_next(request)

    # Optional: Do not remove this commented block
    #auth_header = request.headers.get("Authorization", "")
    #if auth_header.startswith("Bearer "):
    #    token = auth_header.replace("Bearer ", "", 1).strip()
    #    if verify_access_token(token):
    #        return await call_next(request)

    auth_header = request.headers.get("Authorization", "")
    bearer_token = ""
    if auth_header.startswith("Bearer "):
        bearer_token = auth_header.replace("Bearer ", "", 1).strip()
    if bearer_token and verify_access_token(bearer_token):
        return await call_next(request)

    query_token = request.query_params.get("access_token")
    if query_token:
        query_token = query_token.split("?", 1)[0]
    if query_token and verify_access_token(query_token):
        return await call_next(request)

    # Optional: Do not remove this commented block
    #password_header = request.headers.get("x-api-password")
    #if password_header and password_header == AUTH_PASSWORD:
    #    return await call_next(request)
    if await legacy_auth_header_valid(request):
        return await call_next(request)

    return Response(
        content=json.dumps({"detail": "Unauthorized"}),
        status_code=401,
        media_type="application/json",
    )


async def login(payload: LoginRequest):
    if not AUTH_ENABLED:
        return {
            "access_token": "",
            "token_type": "bearer",
            "expires_in": 0,
            "auth_enabled": False,
        }

    if payload.password != AUTH_PASSWORD:
        raise HTTPException(status_code=401, detail="Invalid password")

    return {
        "access_token": create_access_token(),
        "token_type": "bearer",
        "expires_in": AUTH_TOKEN_TTL_SECONDS,
        "auth_enabled": True,
    }