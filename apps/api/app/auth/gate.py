"""Login gate for the whole API when AUU_AUTH is on.

With accounts enabled every /api/* request and the /api/v1/ws websocket need a
valid session cookie, except health and the login/register flow. Writes that
change the shared system engine (strategy params, autopaper toggle, watch list,
risk/pipeline/paper test orders, live switches) also need an admin. Per-user
routes (trade/orders, wallet, password) keep their own checks. Cross-site writes
and websocket handshakes (Origin not this site) are refused (CSRF). AUU_AUTH=off
(local single-user mode) leaves everything as before.
"""
from __future__ import annotations

import json
import os
from typing import Any, Optional
from urllib.parse import urlsplit

from starlette.requests import HTTPConnection

from app.auth.accounts import auth_enabled, user_from_token

COOKIE = "auu_session"

PUBLIC_PATHS = frozenset(
    {
        "/api/v1/health",
        "/api/v1/auth/me",
        "/api/v1/auth/login",
        "/api/v1/auth/logout",
        "/api/v1/auth/register",
        "/api/v1/auth/email/code",
        "/api/v1/auth/password/reset",
    }
)

ADMIN_WRITE_PREFIXES = (
    "/api/v1/strategy/",
    "/api/v1/watch/",
    "/api/v1/paper/",
    "/api/v1/risk/",
    "/api/v1/pipeline/",
    "/api/v1/live/",
)

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def _normalize(path: str) -> str:
    path = path or "/"
    while "//" in path:
        path = path.replace("//", "/")
    if len(path) > 1:
        path = path.rstrip("/")
    return path


def _allowed_origins() -> set[str]:
    raw = os.getenv("CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173")
    return {o.strip().rstrip("/").lower() for o in raw.split(",") if o.strip()}


def csrf_problem(method: str, headers: dict[str, str]) -> Optional[str]:
    """Reject cross-site writes and websocket handshakes (cookie is also SameSite=Lax).

    A browser always sends Origin on cross-origin writes/websockets; it must be this site
    (same host as the request) or a configured CORS origin. With no Origin, a
    Sec-Fetch-Site of cross-site is refused too. Non-browser clients without both pass.
    """
    if (method or "GET").upper() in SAFE_METHODS:
        return None
    origin = (headers.get("origin") or "").strip().rstrip("/").lower()
    if origin:
        if origin == "null":
            return "CSRF_ORIGIN"
        host = (headers.get("host") or "").strip().lower()
        if host and urlsplit(origin).netloc == host:
            return None
        if origin in _allowed_origins():
            return None
        return "CSRF_ORIGIN"
    if (headers.get("sec-fetch-site") or "").strip().lower() == "cross-site":
        return "CSRF_ORIGIN"
    return None


def gate_decision(path: str, method: str, user: Optional[dict[str, Any]]) -> Optional[tuple[int, str, str]]:
    """Return (status, code, message) to reject, or None to allow."""
    path = _normalize(path)
    if not (path == "/api" or path.startswith("/api/")):
        return None
    method = (method or "GET").upper()
    if method == "OPTIONS" or path in PUBLIC_PATHS:
        return None
    if user is None:
        return (401, "AUTH_REQUIRED", "请先登录")
    if method not in SAFE_METHODS and any((path + "/").startswith(p) for p in ADMIN_WRITE_PREFIXES):
        if not user.get("is_admin"):
            return (403, "ADMIN_REQUIRED", "只有管理员可以修改系统设置")
    return None


class AuthGateMiddleware:
    """Pure ASGI so it covers both HTTP and the websocket handshake."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        kind = scope.get("type")
        if kind not in {"http", "websocket"} or not auth_enabled():
            await self.app(scope, receive, send)
            return
        conn = HTTPConnection(scope)
        method = scope.get("method", "GET") if kind == "http" else "GET"
        path = _normalize(scope.get("path", ""))
        if path == "/api" or path.startswith("/api/"):
            headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers") or []}
            # A websocket handshake is a GET, but it carries the cookie like a write.
            if csrf_problem("POST" if kind == "websocket" else method, headers):
                await self._reject(kind, send, 403, "CSRF_ORIGIN", "跨站请求被拒绝")
                return
        user = None
        if _normalize(scope.get("path", "")) not in PUBLIC_PATHS:
            try:
                user = user_from_token(conn.cookies.get(COOKIE) or "")
            except Exception:
                user = None
        verdict = gate_decision(scope.get("path", ""), method, user)
        if verdict is None:
            await self.app(scope, receive, send)
            return
        status, code, message = verdict
        await self._reject(kind, send, status, code, message)

    @staticmethod
    async def _reject(kind, send, status: int, code: str, message: str) -> None:
        if kind == "websocket":
            # Reject the handshake (client sees HTTP 403) without streaming anything.
            await send({"type": "websocket.close", "code": 1008, "reason": code})
            return
        body = json.dumps({"ok": False, "error": {"code": code, "message": message}}, ensure_ascii=False).encode()
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/json; charset=utf-8"),
                    (b"content-length", str(len(body)).encode()),
                    (b"cache-control", b"no-store"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
