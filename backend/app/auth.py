"""Single-admin authentication: password from Secrets + signed session cookie."""
from __future__ import annotations

import asyncio
import hmac
import time
from collections import defaultdict, deque

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.config import env

router = APIRouter(prefix="/api/auth", tags=["auth"])
_failures: dict[str, deque] = defaultdict(deque)
WINDOW_S, MAX_FAILURES = 300, 5


class LoginBody(BaseModel):
    password: str


def _client(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def require_auth(request: Request) -> None:
    if request.session.get("user") != "admin":
        raise HTTPException(status_code=401, detail="Login required")


@router.post("/login")
async def login(body: LoginBody, request: Request) -> dict:
    configured = env().admin_password
    if not configured:
        raise HTTPException(503, "ADMIN_PASSWORD is not set. Add it in Replit Secrets and restart.")
    ip = _client(request)
    q = _failures[ip]
    now = time.monotonic()
    while q and now - q[0] > WINDOW_S:
        q.popleft()
    if len(q) >= MAX_FAILURES:
        raise HTTPException(429, "Too many failed attempts. Try again in a few minutes.")
    if not hmac.compare_digest(body.password.encode(), configured.encode()):
        q.append(now)
        await asyncio.sleep(1.0)
        raise HTTPException(401, "Wrong password")
    q.clear()
    request.session["user"] = "admin"
    return {"authenticated": True}


@router.post("/logout")
async def logout(request: Request) -> dict:
    request.session.clear()
    return {"authenticated": False}


@router.get("/me")
async def me(request: Request) -> dict:
    return {"authenticated": request.session.get("user") == "admin",
            "configured": bool(env().admin_password)}


AuthDep = Depends(require_auth)
