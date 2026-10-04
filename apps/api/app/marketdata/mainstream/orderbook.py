"""Read-only spot order book for the trade page (display only; paper fills never use it).

Outbound pressure and memory are bounded on purpose:
- only coins in the configured universe (``cfg.all_symbols()``) are ever fetched or cached;
- one cached snapshot per coin (``FETCH_LEVELS`` levels per side, a few KB total);
- a request gets the cached snapshot and, if that is older than the TTL, starts at most one
  background refresh for that coin (no viewer = no requests). Only when the snapshot is *stale*
  (none yet, other venue, or older than 4 x TTL, e.g. the first view after switching coins or
  returning to the tab) does the request wait for that refresh, bounded by
  ``AUU_MAINSTREAM_BOOK_WAIT_MS`` (default 1500 ms, 0 = never wait); otherwise it never waits;
- a global budget caps refreshes per minute across all coins, and a failing coin backs off;
- refreshes reuse the on-demand ccxt client (markets already loaded, no extra client in memory;
  ccxt's per-client rate limiter is shared with chart requests) but take their own lock, so a
  slow chart backfill never freezes the book and book fetches never run in parallel.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from collections import deque
from typing import Any, Optional

log = logging.getLogger(__name__)

FETCH_LEVELS = 20  # accepted by both Binance (5/10/20/...) and OKX spot (1..400)
MAX_DEPTH = 20
BACKOFF_MS = 60_000
MAX_SLOTS = 48  # pool coins + recently viewed 大盘 coins


def _env_int(name: str, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(os.getenv(name, str(default)))))
    except ValueError:
        return default


def book_ttl_ms() -> int:
    return _env_int("AUU_MAINSTREAM_BOOK_SEC", 3, 2, 60) * 1000


def book_wait_ms() -> int:
    """Longest a request waits for a refresh of a stale snapshot (0 = serve the stale snapshot at once)."""
    return _env_int("AUU_MAINSTREAM_BOOK_WAIT_MS", 1500, 0, 5000)


def book_budget_per_min() -> int:
    return _env_int("AUU_MAINSTREAM_BOOK_PER_MIN", 40, 1, 600)


def books_enabled() -> bool:
    return os.getenv("AUU_MAINSTREAM_BOOK", "on").strip().lower() not in {"0", "false", "off", "no"}


def _levels(rows: Any, n: int) -> list[list[float]]:
    out: list[list[float]] = []
    for r in rows or []:
        try:
            p, q = float(r[0]), float(r[1])
        except (TypeError, ValueError, IndexError):
            continue
        if p > 0 and q > 0:
            out.append([p, q])
        if len(out) >= n:
            break
    return out


class BookCache:
    def __init__(self, svc: Any):
        self.svc = svc
        self._lock = threading.Lock()
        self._books: dict[str, dict] = {}
        self._starts: deque[int] = deque()
        self._net = threading.Lock()  # one book fetch in flight at a time, across all coins

    def _slot(self, base: str) -> dict:
        b = self._books.get(base)
        if b is None:
            if len(self._books) >= MAX_SLOTS:  # bounded: drop the least recently fetched idle slot
                idle = [k for k, v in self._books.items() if not v["busy"]]
                if idle:
                    self._books.pop(min(idle, key=lambda k: self._books[k]["at"]), None)
            b = self._books[base] = {"at": 0, "ex": None, "bids": [], "asks": [], "ts": None,
                                     "error": None, "try_at": 0, "busy": False, "done": None}
        return b

    def _budget_ok(self, now: int) -> bool:
        while self._starts and now - self._starts[0] >= 60_000:
            self._starts.popleft()
        return len(self._starts) < book_budget_per_min()

    def get(self, base: str, depth: int = 12, ex: Optional[str] = None) -> dict:
        """Pool coin on the strategy source (``ex=None``), or an already-validated coin/venue."""
        svc = self.svc
        depth = max(1, min(MAX_DEPTH, int(depth)))
        if ex is None:
            if base not in svc.cfg.all_symbols():
                raise KeyError(base)
            ex = svc.exchange_for_read()
            key = base
        else:
            key = f"{ex}:{base}"
        now = svc.now_ms()
        start = False
        done: Optional[threading.Event] = None
        with self._lock:
            b = self._slot(key)
            stale_before = b["ex"] != ex or not b["at"] or now - int(b["at"]) > 4 * book_ttl_ms()
            if ex and books_enabled():
                due = (b["ex"] != ex or now - int(b["at"]) >= book_ttl_ms()) and now >= int(b["try_at"]) and not b["busy"]
                if due and self._budget_ok(now):
                    b["busy"] = True
                    b["done"] = threading.Event()
                    self._starts.append(now)
                    start = True
            if stale_before and b["busy"]:
                done = b["done"]
            snap = {k: b[k] for k in ("at", "ex", "ts", "error", "busy")}
            bids, asks = b["bids"][:depth], b["asks"][:depth]
        if start:
            threading.Thread(target=self._refresh, args=(base, ex, key), name=f"mainstream-book-{base}", daemon=True).start()
        waited = None
        wait_ms = book_wait_ms()
        if done is not None and wait_ms > 0:
            t0 = time.monotonic()
            done.wait(wait_ms / 1000)
            waited = int((time.monotonic() - t0) * 1000)
            now = svc.now_ms()
            with self._lock:
                b = self._slot(key)
                snap = {k: b[k] for k in ("at", "ex", "ts", "error", "busy")}
                bids, asks = b["bids"][:depth], b["asks"][:depth]
        same = snap["ex"] == ex
        if not same:
            bids, asks = [], []
        mid = spread_bp = None
        if bids and asks:
            mid = (bids[0][0] + asks[0][0]) / 2
            spread_bp = (asks[0][0] - bids[0][0]) / mid * 1e4
        age = now - int(snap["at"]) if snap["at"] and same else None
        return {
            "symbol": base,
            "pair": svc.cfg.spot(base) if key == base else f"{base}/USDT",
            "exchange": ex,
            "enabled": books_enabled(),
            "depth": depth,
            "bids": bids,
            "asks": asks,
            "mid": mid,
            "spreadBp": spread_bp,
            "ts": snap["ts"] if same else None,
            "fetchedAt": snap["at"] if same and snap["at"] else None,
            "ageMs": age,
            "stale": age is None or age > 4 * book_ttl_ms(),
            "pending": bool(snap["busy"]) and not bids,
            # a refresh is still in flight after the (bounded) wait: the client may re-poll sooner
            "refreshing": bool(snap["busy"]),
            "waitedMs": waited,
            "error": snap["error"],
            "ttlSec": book_ttl_ms() // 1000,
            "note": "display_only",
        }

    def _refresh(self, base: str, ex: str, key: Optional[str] = None) -> None:
        key = key or base
        svc = self.svc
        try:
            with self._net:
                # single attempt, no retry/sleep while holding the lock: the next viewer poll retries
                raw = svc._od_fetcher(ex).client.fetch_order_book(f"{base}/USDT" if key != base else svc.cfg.spot(base), FETCH_LEVELS) or {}
            bids = _levels(raw.get("bids"), MAX_DEPTH)
            asks = _levels(raw.get("asks"), MAX_DEPTH)
            ts = raw.get("timestamp")
            with self._lock:
                b = self._slot(key)
                b.update(at=svc.now_ms(), ex=ex, bids=bids, asks=asks, ts=int(ts) if ts else None,
                         error=None, try_at=0, busy=False)
                if b.get("done") is not None:
                    b["done"].set()
        except Exception as exc:  # keep serving the last snapshot; back off this coin
            with self._lock:
                b = self._slot(key)
                b.update(error=f"{type(exc).__name__}: {str(exc)[:120]}", try_at=svc.now_ms() + BACKOFF_MS, busy=False)
                if b.get("done") is not None:
                    b["done"].set()
            log.warning("mainstream book %s/%s failed: %s", ex, base, exc)


_cache: Optional[BookCache] = None
_clock = threading.Lock()


def get_book_cache() -> BookCache:
    global _cache
    from app.marketdata.mainstream import get_service

    svc = get_service()
    with _clock:
        if _cache is None or _cache.svc is not svc:
            _cache = BookCache(svc)
        return _cache
