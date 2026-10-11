"""Rate limits, scraper detection and bad-client filtering for /api (pure ASGI, in-memory).

Client IP: the trust chain is Cloudflare -> nginx -> uvicorn. nginx only trusts
``CF-Connecting-IP`` from Cloudflare's published ranges (``set_real_ip_from`` +
``real_ip_header``; mTLS / Authenticated Origin Pulls make the origin reachable only
through Cloudflare), passes the result as ``X-Forwarded-For``, and uvicorn only
honours that header from 127.0.0.1 (``forwarded_allow_ips`` default). So
``scope["client"]`` already is the real client. Any ``CF-Connecting-IP`` a client
sends to the app directly is ignored on purpose.

Limits (sliding windows, one process, reset on restart):

* every /api request: per IP and per signed-in account (``AUU_RL_*``);
* data reads (market, strategy, news, points, leaderboard ...): a scraper check counts
  *distinct* URLs per account/IP in 10 minutes (polling the same URL does not count;
  ``since`` is ignored because the console poll changes it every tick). Over the limit the
  key is soft-banned for ``AUU_RL_BAN_SEC`` (429 + Retry-After, nothing permanent);
* writes: per account;
* obvious automation user agents (python-requests, curl, scrapy, Go, Java ...) and an
  empty UA get 403 on the gated API, except health/status.

Loopback callers (auu-guard, the server's own self-checks) are never limited. The
strategy runner, shadows, recon, news and the HK ledger sync never make HTTP calls to
this API (they run in-process / over ssh), so they cannot be affected.
``AUU_SHIELD=off`` disables everything (local dev / tests that need it).
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from collections import deque
from typing import Optional
from urllib.parse import parse_qsl, urlencode

from starlette.requests import HTTPConnection

_LOG = logging.getLogger("auu.shield")

LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost"})
# Health/status stay reachable for monitors; auth endpoints have their own limits too.
UA_EXEMPT = frozenset({"/api/v1/health", "/api/v1/status"})
DATA_PREFIXES = (
    "/api/v1/mainstream/",
    "/api/v1/news",
    "/api/v1/majors",
    "/api/v1/markets",
    "/api/v1/leaderboard",
    "/api/v1/stats/",
    "/api/v1/events",
    "/api/v1/points/",
    "/api/v1/strategy/",
    "/api/v1/candles",
    "/api/v1/universe",
    "/api/v1/search",
)
IGNORED_QUERY = frozenset({"since", "_", "t", "ts"})
BAD_UA = re.compile(
    r"(python-requests|python-urllib|python-httpx|aiohttp|httpx/|scrapy|curl/|wget/|libwww|lwp-|go-http-client|"
    r"java/|okhttp|apache-httpclient|node-fetch|undici|axios/|got \(|phantomjs|slimerjs|nutch|httrack|"
    r"mj12bot|ahrefsbot|semrushbot|dotbot|petalbot|bytespider|gptbot|ccbot|claudebot|amazonbot|"
    r"dataforseobot|blexbot|serpstatbot|zgrab|masscan|nmap|sqlmap|nikto|nuclei|acunetix|wpscan)",
    re.I,
)


def _on(name: str, default: str = "on") -> bool:
    return os.getenv(name, default).strip().lower() not in {"0", "false", "off", "no"}


def _int(name: str, default: int, lo: int = 1) -> int:
    try:
        return max(lo, int(os.getenv(name, str(default))))
    except ValueError:
        return default


def shield_enabled() -> bool:
    return _on("AUU_SHIELD", "on")


def limits() -> dict[str, int]:
    return {
        # all /api requests (normal pages poll ~1-4 req/s per tab; nginx also caps 10 r/s per IP)
        "ip_per_min": _int("AUU_RL_IP_PER_MIN", 900),
        "user_per_min": _int("AUU_RL_USER_PER_MIN", 600),
        # writes (orders, points, settings) per account
        "write_per_min": _int("AUU_RL_WRITE_PER_MIN", 60),
        # scraper check: distinct data URLs per key in 10 min
        "distinct_per_10min": _int("AUU_RL_DISTINCT_10MIN", 400),
        "ban_sec": _int("AUU_RL_BAN_SEC", 900, lo=10),
    }


def client_ip(scope) -> str:
    client = scope.get("client")
    if client and client[0]:
        return str(client[0])
    return "local"


def is_loopback(ip: str) -> bool:
    return ip in LOOPBACK or ip.startswith("127.")


class _Window:
    """Thread-safe sliding-window counters with bounded memory."""

    def __init__(self, max_keys: int = 50_000) -> None:
        self._lock = threading.Lock()
        self._hits: dict[str, deque] = {}
        self._distinct: dict[str, dict[str, float]] = {}
        self._bans: dict[str, float] = {}
        self._max_keys = max_keys

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()
            self._distinct.clear()
            self._bans.clear()

    def _gc(self, now: float) -> None:
        if len(self._hits) > self._max_keys:
            for k in [k for k, q in self._hits.items() if not q or now - q[-1] > 600]:
                self._hits.pop(k, None)
        if len(self._distinct) > self._max_keys:
            for k in [k for k, d in self._distinct.items() if not d or now - max(d.values()) > 600]:
                self._distinct.pop(k, None)
        for k in [k for k, until in self._bans.items() if until <= now]:
            self._bans.pop(k, None)

    def hit(self, key: str, limit: int, window: float, now: float) -> bool:
        """Count one hit; False when the key is over ``limit`` in ``window`` seconds."""
        with self._lock:
            q = self._hits.get(key)
            if q is None:
                q = self._hits[key] = deque()
                self._gc(now)
            while q and now - q[0] >= window:
                q.popleft()
            if len(q) >= limit:
                return False
            q.append(now)
            return True

    def distinct(self, key: str, item: str, window: float, now: float) -> int:
        with self._lock:
            d = self._distinct.get(key)
            if d is None:
                d = self._distinct[key] = {}
                self._gc(now)
            if len(d) > 64 and len(d) % 64 == 0:
                for k in [k for k, t in d.items() if now - t >= window]:
                    d.pop(k, None)
            d[item] = now
            return sum(1 for t in d.values() if now - t < window)

    def banned(self, key: str, now: float) -> float:
        with self._lock:
            until = self._bans.get(key, 0.0)
            return until - now if until > now else 0.0

    def ban(self, key: str, seconds: float, now: float) -> None:
        with self._lock:
            self._bans[key] = now + seconds


STATE = _Window()


def reset_shield() -> None:
    STATE.reset()


def _url_key(path: str, query: str) -> str:
    pairs = sorted((k, v) for k, v in parse_qsl(query or "", keep_blank_values=True) if k not in IGNORED_QUERY)
    return path + ("?" + urlencode(pairs) if pairs else "")


def decide(*, path: str, method: str, query: str, ip: str, user_id: Optional[str], ua: str,
           now: Optional[float] = None) -> Optional[tuple[int, str, str, int]]:
    """Return (status, code, message, retry_after) to reject, or None to allow."""
    if not (path == "/api" or path.startswith("/api/")):
        return None
    if is_loopback(ip):
        return None
    now = time.time() if now is None else now
    cfg = limits()
    method = (method or "GET").upper()
    if path not in UA_EXEMPT and (not ua.strip() or BAD_UA.search(ua)):
        return (403, "CLIENT_BLOCKED", "该客户端不允许访问接口，请使用浏览器", 0)
    keys = [f"ip:{ip}"] + ([f"u:{user_id}"] if user_id else [])
    for key in keys:
        wait = STATE.banned(key, now)
        if wait > 0:
            return (429, "SCRAPE_SUSPECT", "请求过于频繁或疑似批量抓取，已临时限制，请稍后再试", int(wait) + 1)
    if not STATE.hit(f"ip:{ip}", cfg["ip_per_min"], 60.0, now):
        return (429, "RATE_LIMIT", "请求过于频繁，请稍后再试", 60)
    if user_id and not STATE.hit(f"u:{user_id}", cfg["user_per_min"], 60.0, now):
        return (429, "RATE_LIMIT", "请求过于频繁，请稍后再试", 60)
    if method not in {"GET", "HEAD", "OPTIONS"} and user_id:
        if not STATE.hit(f"w:{user_id}", cfg["write_per_min"], 60.0, now):
            return (429, "RATE_LIMIT", "操作过于频繁，请稍后再试", 60)
    if method == "GET" and any(path == p.rstrip("/") or path.startswith(p) for p in DATA_PREFIXES):
        item = _url_key(path, query)
        for key in keys:
            if STATE.distinct(f"d:{key}", item, 600.0, now) > cfg["distinct_per_10min"]:
                STATE.ban(key, cfg["ban_sec"], now)
                _LOG.warning("shield soft-ban %s for %ss (distinct data URLs > %s/10min)", key, cfg["ban_sec"],
                             cfg["distinct_per_10min"])
                return (429, "SCRAPE_SUSPECT", "请求过于频繁或疑似批量抓取，已临时限制，请稍后再试", cfg["ban_sec"])
    return None


class ShieldMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") not in {"http", "websocket"} or not shield_enabled():
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "") or "/"
        while "//" in path:
            path = path.replace("//", "/")
        if len(path) > 1:
            path = path.rstrip("/")
        if not (path == "/api" or path.startswith("/api/")):
            await self.app(scope, receive, send)
            return
        conn = HTTPConnection(scope)
        uid = None
        try:
            from app.auth.accounts import auth_enabled, user_from_token

            if auth_enabled():
                user = user_from_token(conn.cookies.get("auu_session") or "")
                uid = str(user["id"]) if user else None
        except Exception:
            uid = None
        method = scope.get("method", "GET") if scope.get("type") == "http" else "GET"
        query = (scope.get("query_string") or b"").decode("latin-1")
        verdict = decide(path=path, method=method, query=query, ip=client_ip(scope), user_id=uid,
                         ua=conn.headers.get("user-agent", ""))
        if verdict is None:
            await self.app(scope, receive, send)
            return
        status, code, message, retry = verdict
        if scope.get("type") == "websocket":
            await send({"type": "websocket.close", "code": 1008, "reason": code})
            return
        body = json.dumps({"ok": False, "error": {"code": code, "message": message, **({"retry_after": retry} if retry else {})}},
                          ensure_ascii=False).encode()
        headers = [(b"content-type", b"application/json; charset=utf-8"), (b"content-length", str(len(body)).encode()),
                   (b"cache-control", b"no-store")]
        if retry:
            headers.append((b"retry-after", str(retry).encode()))
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": body})


API_HEADERS = (
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"strict-origin-when-cross-origin"),
    (b"cross-origin-resource-policy", b"same-origin"),
)


class ApiHeadersMiddleware:
    """Security headers on every /api response (nginx adds them too; this covers direct/dev use).

    API JSON is never cached by browsers or shared caches unless the route set its own policy.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or not (scope.get("path", "") or "").startswith("/api"):
            await self.app(scope, receive, send)
            return

        async def wrapped(message):
            if message.get("type") == "http.response.start":
                headers = list(message.get("headers") or [])
                have = {k.lower() for k, _ in headers}
                for k, v in API_HEADERS:
                    if k not in have:
                        headers.append((k, v))
                if b"cache-control" not in have:
                    headers.append((b"cache-control", b"no-store"))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, wrapped)
