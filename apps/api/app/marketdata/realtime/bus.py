"""In-memory market bus: latest ticker / forming bar per exchange + coalescing fan-out to /api/v1/ws clients.

Two kinds of reader:

* the REST read path (``MainstreamService`` runs in worker threads) asks for a snapshot; state is guarded by a
  ``threading.Lock`` held only for dict copies (microseconds), never across I/O;
* websocket sessions (event-loop only) hold a :class:`Subscriber`. ``publish`` stores the newest pre-serialised
  frame per key in the subscriber's ``pending`` dict and wakes its sender. A slow client therefore never queues
  more than one frame per subscribed key (latest wins, older ones are counted as ``coalesced``): memory per
  connection is bounded by its subscriptions, and the feed is never slowed down by a client (backpressure stops
  at the subscriber, the send itself has a timeout in the session).
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
from typing import Any, Iterable, Optional

Key = tuple  # ("ticker", base) | ("kline", base, tf) | ("status",)


def now_ms() -> int:
    return int(time.time() * 1000)


def dumps(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


class Subscriber:
    __slots__ = ("keys", "pending", "event", "coalesced", "sent", "meta")

    def __init__(self, meta: Optional[dict] = None) -> None:
        self.keys: set[Key] = set()
        self.pending: dict[Key, str] = {}
        self.event = asyncio.Event()
        self.coalesced = 0
        self.sent = 0
        self.meta = meta or {}

    def offer(self, key: Key, frame: str) -> None:
        if key in self.pending:
            self.coalesced += 1
        self.pending[key] = frame
        self.event.set()

    def drain(self) -> list[str]:
        if not self.pending:
            return []
        frames = list(self.pending.values())
        self.pending = {}
        return frames


class FeedState:
    """Liveness of one exchange connection (``feed_id`` e.g. ``binance`` / ``okx:tickers``)."""

    __slots__ = ("feed_id", "ex", "carries", "connected", "url", "connected_at", "last_msg_ms", "last_data_ms",
                 "connects", "disconnects", "messages", "parse_errors", "last_error", "suspect_until_ms")

    def __init__(self, feed_id: str, ex: str, carries: Iterable[str]) -> None:
        self.feed_id, self.ex, self.carries = feed_id, ex, frozenset(carries)
        self.connected = False
        self.url: Optional[str] = None
        self.connected_at: Optional[int] = None
        self.last_msg_ms = 0
        self.last_data_ms = 0
        self.connects = 0
        self.disconnects = 0
        self.messages = 0
        self.parse_errors = 0
        self.last_error: Optional[str] = None
        self.suspect_until_ms = 0

    def view(self, now: int, stale_ms: int) -> dict:
        return {
            "feed": self.feed_id, "exchange": self.ex, "carries": sorted(self.carries), "connected": self.connected,
            "url": self.url, "connectedAt": self.connected_at, "lastDataMs": self.last_data_ms or None,
            "dataAgeMs": (now - self.last_data_ms) if self.last_data_ms else None,
            "fresh": self.fresh(now, stale_ms), "connects": self.connects, "disconnects": self.disconnects,
            "messages": self.messages, "parseErrors": self.parse_errors, "lastError": self.last_error,
            "suspectUntilMs": self.suspect_until_ms or None,
        }

    def fresh(self, now: int, stale_ms: int) -> bool:
        return bool(self.connected and self.last_data_ms and now - self.last_data_ms <= stale_ms
                    and now >= self.suspect_until_ms)


class MarketBus:
    def __init__(self, *, stale_ms: int = 10_000, clock=now_ms) -> None:
        self.stale_ms = stale_ms
        self.clock = clock
        self._lock = threading.Lock()
        self._feeds: dict[str, FeedState] = {}
        self._tickers: dict[str, dict[str, dict]] = {}  # ex -> base -> {last, change24h, quoteVolume, ts, at}
        self._bars: dict[tuple, dict] = {}  # (ex, base, tf) -> {"cur": row, "prev": row|None, "at": ms}
        self._index: dict[Key, set[Subscriber]] = {}  # loop thread only
        self.published = 0

    # ---- feed state ------------------------------------------------------------------------
    def feed(self, feed_id: str, ex: str, carries: Iterable[str]) -> FeedState:
        with self._lock:
            st = self._feeds.get(feed_id)
            if st is None:
                st = self._feeds[feed_id] = FeedState(feed_id, ex, carries)
            return st

    def feed_states(self) -> list[FeedState]:
        with self._lock:
            return list(self._feeds.values())

    def fresh(self, ex: str, kind: str, now: Optional[int] = None) -> bool:
        now = self.clock() if now is None else now
        with self._lock:
            return any(s.ex == ex and kind in s.carries and s.fresh(now, self.stale_ms) for s in self._feeds.values())

    def data_at(self, ex: str, kind: str) -> int:
        """Newest data time of the (fresh) connection carrying ``kind`` for ``ex``."""
        with self._lock:
            return max((s.last_data_ms for s in self._feeds.values() if s.ex == ex and kind in s.carries), default=0)

    def mark_suspect(self, ex: str, until_ms: int) -> None:
        with self._lock:
            for s in self._feeds.values():
                if s.ex == ex:
                    s.suspect_until_ms = until_ms

    # ---- state updates (feed side, event loop) ---------------------------------------------
    def set_ticker(self, ex: str, base: str, row: dict) -> None:
        with self._lock:
            self._tickers.setdefault(ex, {})[base] = row

    def set_bar(self, ex: str, base: str, tf: str, row: list, closed: bool, at: int) -> dict:
        """Keep the forming bar and the bar before it (what the REST tail fetches with limit=2)."""
        key = (ex, base, tf)
        with self._lock:
            b = self._bars.get(key)
            if b is None:
                b = self._bars[key] = {"cur": row, "prev": None, "at": at, "closed": closed}
            elif int(row[0]) > int(b["cur"][0]):
                b.update(prev=b["cur"], cur=row, at=at, closed=closed)
            elif int(row[0]) == int(b["cur"][0]):
                b.update(cur=row, at=at, closed=closed)
            elif b["prev"] is None or int(row[0]) >= int(b["prev"][0]):
                b["prev"] = row  # late update of the previous bar
            return dict(b)

    # ---- REST read path (any thread) -------------------------------------------------------
    def ticker(self, ex: str, base: str) -> Optional[dict]:
        with self._lock:
            t = (self._tickers.get(ex) or {}).get(base)
            return dict(t) if t else None

    def ticker_snapshot(self, ex: str, bases: list[str], now: Optional[int] = None) -> Optional[dict]:
        """A REST-ticker-cache-shaped dict (``at``/``ex``/``data``) when the stream for ``ex`` is fresh and every
        requested coin has been seen on this connection; otherwise None (caller uses REST)."""
        now = self.clock() if now is None else now
        if not self.fresh(ex, "ticker", now):
            return None
        with self._lock:
            src = self._tickers.get(ex) or {}
            conn_at = max((s.connected_at or 0) for s in self._feeds.values() if s.ex == ex and "ticker" in s.carries)
            data = {}
            for b in bases:
                t = src.get(b)
                if not t or int(t["at"]) < conn_at:  # not (re)confirmed since this connection opened
                    return None
                data[b] = {"last": t["last"], "change24h": t["change24h"], "quoteVolume": t["quoteVolume"], "ts": t["ts"]}
        return {"at": self.data_at(ex, "ticker"), "ex": ex, "data": data, "source": "ws"}

    def tail(self, ex: str, base: str, tf: str, step_ms: int, now: Optional[int] = None) -> Optional[dict]:
        """Forming bar (+ previous) in the REST tail shape, only while fresh and covering the current period."""
        now = self.clock() if now is None else now
        if not self.fresh(ex, "kline", now):
            return None
        with self._lock:
            b = self._bars.get((ex, base, tf))
            conn_at = max((s.connected_at or 0) for s in self._feeds.values() if s.ex == ex and "kline" in s.carries)
            if not b or int(b["at"]) < conn_at or int(b["cur"][0]) != now // step_ms * step_ms:
                return None
            rows = ([list(b["prev"])] if b["prev"] else []) + [list(b["cur"])]
        at = self.data_at(ex, "kline")
        return {"at": at, "tried": at, "rows": rows, "error": None, "source": "ws"}

    # ---- fan-out (event loop only) ---------------------------------------------------------
    def add(self, sub: Subscriber, key: Key) -> None:
        sub.keys.add(key)
        self._index.setdefault(key, set()).add(sub)

    def discard(self, sub: Subscriber, key: Key) -> None:
        sub.keys.discard(key)
        sub.pending.pop(key, None)
        s = self._index.get(key)
        if s is not None:
            s.discard(sub)
            if not s:
                self._index.pop(key, None)

    def drop(self, sub: Subscriber) -> None:
        for key in list(sub.keys):
            self.discard(sub, key)

    def publish(self, key: Key, frame: str) -> int:
        subs = self._index.get(key)
        if not subs:
            return 0
        for s in subs:
            s.offer(key, frame)
        self.published += 1
        return len(subs)

    def subscribed_keys(self) -> list[Key]:
        return list(self._index)

    def subscriber_count(self) -> int:
        seen: set[int] = set()
        for subs in self._index.values():
            seen.update(id(s) for s in subs)
        return len(seen)
