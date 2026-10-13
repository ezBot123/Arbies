"""Shared API dependencies: runtime access, authentication, rate limiting."""

from __future__ import annotations

from datetime import timedelta

from fastapi import Depends, HTTPException, Request, WebSocket

from arbies.core.models import utcnow
from arbies.db.models import AuthSession
from arbies.security import token_hash

COOKIE = "arbies_session"


def get_rt(request: Request):
    return request.app.state.rt


async def session_user(rt, token: str | None) -> str | None:
    if not token:
        return None
    async with rt.db.session() as s:
        row = await s.get(AuthSession, token_hash(token))
        if row is None or row.expires_at < utcnow():
            return None
        return row.username


def _token_from_request(request: Request) -> str | None:
    tok = request.cookies.get(COOKIE)
    if tok:
        return tok
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:]
    return None


async def require_user(request: Request, rt=Depends(get_rt)) -> str:
    user = await session_user(rt, _token_from_request(request))
    if user is None:
        raise HTTPException(status_code=401, detail="authentication required")
    return user


async def ws_user(ws: WebSocket) -> str | None:
    rt = ws.app.state.rt
    tok = ws.cookies.get(COOKIE) or ws.query_params.get("token")
    return await session_user(rt, tok)


def rate_limit(key: str, limit: int, window_s: float):
    async def dep(request: Request, rt=Depends(get_rt)):
        ip = request.client.host if request.client else "unknown"
        if not rt.rate_limiter.allow(f"{key}:{ip}", limit, window_s):
            raise HTTPException(status_code=429, detail="too many requests; slow down")
    return dep


def session_ttl(rt) -> timedelta:
    return timedelta(hours=rt.settings.session_ttl_hours)
