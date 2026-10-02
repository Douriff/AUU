"""ccxt public-endpoint wrapper with retries, pagination and block detection.

Only public market-data methods are called (fetch_ohlcv, fetch_funding_rate_history,
fetch_funding_rate). No API key is ever set on the exchange object.
"""
from __future__ import annotations

import time
from typing import Any, Callable, Optional

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


def make_exchange(name: str, cfg: MainstreamConfig) -> Any:
    """Keyless ccxt client (lazy import keeps ccxt out of legacy-only processes)."""
    import ccxt  # MIT

    klass = getattr(ccxt, name)
    return klass(
        {
            "enableRateLimit": True,
            "timeout": cfg.timeout_ms,
            "options": {"defaultType": "spot", "fetchMarkets": {"types": _MARKET_TYPES.get(name, ["spot"])}},
        }
    )


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
