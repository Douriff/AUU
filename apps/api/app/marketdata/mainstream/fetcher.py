"""ccxt public-endpoint wrapper with retries, pagination and block detection.

Only public market-data methods are called (fetch_ohlcv, fetch_funding_rate_history,
fetch_funding_rate). No API key is ever set on the exchange object.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable, Optional
from urllib.parse import urlsplit, urlunsplit

from app.marketdata.mainstream.config import FUNDING_STEP_MS, TF_MS, MainstreamConfig

_MARKET_TYPES = {"binance": ["spot", "linear"], "okx": ["spot", "swap"]}
_PAGE = {"binance": 1000, "okx": 100}
_FUNDING_PAGE = {"binance": 1000, "okx": 100}

# Error classes that will not improve with a retry.
_FATAL = {"BadSymbol", "NotSupported", "AuthenticationError", "PermissionDenied", "BadRequest", "ArgumentsRequired"}
_BLOCK_HINTS = ("451", "restricted location", "unavailable for legal reasons", "403 forbidden", "cloudfront")


class FetchError(RuntimeError):
    def __init__(self, message: str, *, blocked: bool = False, fatal: bool = False):
        super().__init__(message)
        self.blocked = blocked
        self.fatal = fatal


def _names(exc: BaseException) -> set[str]:
    return {c.__name__ for c in type(exc).__mro__}


def classify(exc: BaseException) -> FetchError:
    text = f"{type(exc).__name__}: {exc}"
    low = text.lower()
    blocked = any(h in low for h in _BLOCK_HINTS)
    fatal = blocked or bool(_names(exc) & _FATAL)
    return FetchError(text[:300], blocked=blocked, fatal=fatal)


# ---- Binance REST host selection + failover --------------------------------------------------------
# Hosts that serve the same spot ``/api/*`` endpoints (all share the per-IP weight budget). fapi (perp funding),
# sapi (needs keys) and every other host are never rewritten.
BINANCE_SPOT_FAMILY = frozenset(
    {"api.binance.com", "api1.binance.com", "api2.binance.com", "api3.binance.com", "api4.binance.com", "data-api.binance.vision"}
)
HOST_PRIMARY_RETRY_SEC = 600  # after falling back, probe the preferred host again this often
# ccxt classes that mean "this gateway is unreachable" (a different gateway may work). Rate limits are NOT here:
# the weight budget is per IP, so another host cannot help and retrying elsewhere would only add load.
_HOST_FAILOVER = {"NetworkError", "RequestTimeout", "ExchangeNotAvailable"}
_HOST_NO_FAILOVER = {"RateLimitExceeded", "DDoSProtection", "BadSymbol", "BadRequest", "AuthenticationError", "PermissionDenied", "NotSupported"}


class HostPool:
    """Ordered Binance gateways with a sticky current host (shared by every client in the process).

    A failure of the current host advances to the next one; a host that was left is re-probed after
    ``retry_sec`` so the preferred (fastest) gateway comes back by itself."""

    def __init__(self, hosts: list[str], *, retry_sec: float = HOST_PRIMARY_RETRY_SEC, clock: Callable[[], float] = time.monotonic):
        self.hosts = list(hosts) or ["api.binance.com"]
        self.retry_sec = retry_sec
        self._clock = clock
        self._lock = threading.Lock()
        self._idx = 0
        self._since = clock()
        self.switches = 0

    def current(self) -> str:
        with self._lock:
            if self._idx != 0 and self._clock() - self._since >= self.retry_sec:
                self._idx, self._since = 0, self._clock()  # probe the preferred host again
            return self.hosts[self._idx]

    def fail(self, host: str) -> None:
        with self._lock:
            if self.hosts[self._idx] == host and len(self.hosts) > 1:  # only the first reporter advances
                self._idx = (self._idx + 1) % len(self.hosts)
                self._since = self._clock()
                self.switches += 1


_POOLS: dict[tuple, HostPool] = {}
_POOLS_LOCK = threading.Lock()


def host_pool(hosts: list[str]) -> HostPool:
    key = tuple(hosts)
    with _POOLS_LOCK:
        if key not in _POOLS:
            _POOLS[key] = HostPool(list(hosts))
        return _POOLS[key]


def _failover_worthy(exc: BaseException) -> bool:
    names = _names(exc)
    if names & _HOST_NO_FAILOVER:
        return False
    low = f"{type(exc).__name__}: {exc}".lower()
    return bool(names & _HOST_FAILOVER) or any(h in low for h in _BLOCK_HINTS) or isinstance(exc, (OSError, TimeoutError))


def install_host_failover(client: Any, pool: HostPool) -> Any:
    """Route a keyless ccxt Binance client's spot ``/api/*`` calls through ``pool`` and fall back to the next
    gateway on a network/gateway error (also when one gateway answers 403/451 and another does not). Wraps
    ``client.fetch`` so every caller of the client (Fetcher, order book, extra venues) gets the same behaviour."""
    orig = client.fetch

    def fetch(url, method="GET", headers=None, body=None):
        parts = urlsplit(url)
        if (parts.hostname or "") not in BINANCE_SPOT_FAMILY or not parts.path.startswith("/api/"):
            return orig(url, method, headers, body)
        last: Optional[BaseException] = None
        for _ in range(len(pool.hosts)):
            host = pool.current()
            u = urlunsplit((parts.scheme, host, parts.path, parts.query, parts.fragment))
            try:
                return orig(u, method, headers, body)
            except Exception as exc:
                if not _failover_worthy(exc):
                    raise
                last = exc
                pool.fail(host)
        assert last is not None
        raise last

    client.fetch = fetch
    client._auu_host_pool = pool
    return client


def make_exchange(name: str, cfg: MainstreamConfig) -> Any:
    """Keyless ccxt client (lazy import keeps ccxt out of legacy-only processes)."""
    import ccxt  # MIT

    klass = getattr(ccxt, name)
    client = klass(
        {
            "enableRateLimit": True,
            "timeout": cfg.timeout_ms,
            "options": {"defaultType": "spot", "fetchMarkets": {"types": _MARKET_TYPES.get(name, ["spot"])}},
        }
    )
    if name == "binance" and getattr(cfg, "binance_hosts", None):
        install_host_failover(client, host_pool(cfg.binance_hosts))
    return client


class Fetcher:
    def __init__(
        self,
        name: str,
        client: Any,
        cfg: MainstreamConfig,
        *,
        sleep: Callable[[float], None] = time.sleep,
        now_ms: Callable[[], int] = lambda: int(time.time() * 1000),
    ):
        self.name = name
        self.client = client
        self.cfg = cfg
        self.sleep = sleep
        self.now_ms = now_ms

    def call(self, method: str, *args, **kwargs):
        last: Optional[FetchError] = None
        for attempt in range(self.cfg.retries):
            try:
                return getattr(self.client, method)(*args, **kwargs)
            except Exception as exc:  # ccxt + transport errors
                last = classify(exc)
                if last.fatal:
                    raise last
                if attempt + 1 < self.cfg.retries:
                    self.sleep(self.cfg.retry_base_sec * (2**attempt))
        assert last is not None
        raise last

    def once(self, method: str, *args, **kwargs):
        """One attempt, no retry or sleep (request paths that must not stall); errors are classified like ``call``."""
        try:
            return getattr(self.client, method)(*args, **kwargs)
        except Exception as exc:
            raise classify(exc)

    def ohlcv(self, symbol: str, tf: str, since: int, until: Optional[int] = None) -> list[list]:
        """Paginate forward from ``since`` until caught up (or ``until``)."""
        step = TF_MS[tf]
        page = _PAGE.get(self.name, 300)
        end = until if until is not None else self.now_ms()
        out: list[list] = []
        cursor = int(since)
        for _ in range(200):  # hard stop: 200 pages
            rows = self.call("fetch_ohlcv", symbol, tf, since=cursor, limit=page) or []
            rows = [r for r in rows if r and r[0] is not None and int(r[0]) >= cursor]
            if until is not None:
                rows = [r for r in rows if int(r[0]) <= until]
            if not rows:
                break
            out.extend(rows)
            nxt = int(rows[-1][0]) + step
            if nxt <= cursor or nxt > end:
                break
            cursor = nxt
        return out

    def funding_history(self, symbol: str, since: int) -> list[tuple[int, float]]:
        page = _FUNDING_PAGE.get(self.name, 100)
        out: list[tuple[int, float]] = []
        cursor = int(since)
        end = self.now_ms()
        for _ in range(100):
            rows = self.call("fetch_funding_rate_history", symbol, since=cursor, limit=page) or []
            pts = [
                (int(r["timestamp"]), float(r["fundingRate"]))
                for r in rows
                if r.get("timestamp") is not None and r.get("fundingRate") is not None and int(r["timestamp"]) >= cursor
            ]
            if not pts:
                break
            out.extend(pts)
            nxt = max(p[0] for p in pts) + 1
            if nxt <= cursor or nxt + FUNDING_STEP_MS > end:
                break
            cursor = nxt
        return out

    def funding_now(self, symbol: str) -> dict:
        r = self.call("fetch_funding_rate", symbol) or {}
        return {
            "rate": r.get("fundingRate"),
            "nextFundingMs": r.get("fundingTimestamp") or r.get("nextFundingTimestamp"),
            "markPrice": r.get("markPrice"),
            "indexPrice": r.get("indexPrice"),
            "ts": r.get("timestamp") or self.now_ms(),
        }
