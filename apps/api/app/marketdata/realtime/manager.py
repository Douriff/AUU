"""Runs the exchange feeds, picks the source exchange, cross-checks against REST and pushes frames to the bus.

Source selection (every second): the exchange the REST layer is using (``MainstreamService.active``, Binance
normally) when its stream is fresh, else the first other fresh stream (OKX), else none -> clients get
``rt_status live=false`` and every reader falls back to REST. Frames are only pushed for the selected
exchange, so a client never sees two venues' prices interleaved.

REST cross-check (every ``xcheck_sec``, in a worker thread, one ``ticker/24hr`` request ~ weight 2): a stream
whose prices differ from REST by more than ``xcheck_bps`` on ``xcheck_strikes`` consecutive checks is marked
suspect for two check periods (readers use REST meanwhile).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Optional

from app.marketdata.realtime.bus import MarketBus, Subscriber, dumps, now_ms
from app.marketdata.realtime.config import RtConfig
from app.marketdata.realtime.feeds import BaseFeed, build_feeds

log = logging.getLogger("auu.realtime")

RestFn = Callable[[str], dict]  # exchange -> {base: last}


class RealtimeManager:
    def __init__(self, cfg: RtConfig, *, bases: list[str], quote: str = "USDT", display: Optional[list[str]] = None,
                 preferred: Callable[[], Optional[str]] = lambda: None, rest_fn: Optional[RestFn] = None,
                 feed_kw: Optional[dict] = None, clock: Callable[[], int] = now_ms) -> None:
        self.cfg = cfg
        self.bases = list(bases)
        self.display = list(display or bases)
        self.quote = quote
        self.clock = clock
        self.bus = MarketBus(stale_ms=cfg.stale_ms, clock=clock)
        self.preferred = preferred
        self.rest_fn = rest_fn
        self.feeds: list[BaseFeed] = build_feeds(cfg, self.bases, quote, self.bus, self, **(feed_kw or {}))
        self.selected: Optional[str] = None
        self.running = False
        self.started_at: Optional[int] = None
        self.xcheck: dict[str, dict] = {}
        self._strikes: dict[str, int] = {}
        self._tasks: list[asyncio.Task] = []
        # websocket clients (event loop only)
        self.clients = 0
        self.per_key: dict[str, int] = {}
        self.rejected = 0
        self.slow_closed = 0

    # ---- feed sink (event loop) -------------------------------------------------------------
    def on_ticker(self, ex: str, base: str, row: dict) -> None:
        self.bus.set_ticker(ex, base, row)
        if ex == self.selected:
            self.bus.publish(("ticker", base), self.ticker_frame(ex, base, row))

    def on_bar(self, ex: str, base: str, tf: str, row: list, closed: bool, event_ts: Optional[int], recv: int) -> None:
        self.bus.set_bar(ex, base, tf, row, closed, recv)
        if ex == self.selected:
            self.bus.publish(("kline", base, tf), self.bar_frame(ex, base, tf, row, closed, event_ts, recv))

    @staticmethod
    def ticker_frame(ex: str, base: str, t: dict) -> str:
        return dumps({"type": "ticker", "payload": {"symbol": base, "ex": ex, "last": t["last"], "change24h": t["change24h"],
                                                    "quoteVolume": t["quoteVolume"], "ts": t["ts"], "recvAt": t["at"]}})

    @staticmethod
    def bar_frame(ex: str, base: str, tf: str, row: list, closed: bool, event_ts: Optional[int], recv: int) -> str:
        return dumps({"type": "kline", "payload": {"symbol": base, "tf": tf, "ex": ex, "ts": row[0], "open": row[1],
                                                   "high": row[2], "low": row[3], "close": row[4], "volume": row[5],
                                                   "closed": closed, "eventTs": event_ts, "recvAt": recv}})

    def status_frame(self) -> str:
        return dumps({"type": "rt_status", "payload": {"live": self.selected is not None, "source": self.selected,
                                                       "staleMs": self.cfg.stale_ms, "at": self.clock()}})

    def snapshot_frame(self, key: tuple) -> Optional[str]:
        ex = self.selected
        if ex is None:
            return None
        if key[0] == "ticker":
            t = self.bus.ticker(ex, key[1])
            return self.ticker_frame(ex, key[1], t) if t else None
        if key[0] == "kline":
            with self.bus._lock:
                b = self.bus._bars.get((ex, key[1], key[2]))
                b = dict(b) if b else None
            if b:
                return self.bar_frame(ex, key[1], key[2], b["cur"], bool(b.get("closed")), None, int(b["at"]))
        return None

    # ---- selection --------------------------------------------------------------------------
    def choose(self, now: Optional[int] = None) -> Optional[str]:
        now = self.clock() if now is None else now
        pref = self.preferred()
        order = list(self.cfg.exchanges)
        if pref in order:
            order.remove(pref)
            order.insert(0, pref)
        for ex in order:
            if self.bus.fresh(ex, "ticker", now):
                return ex
        return None

    def reselect(self) -> None:
        new = self.choose()
        if new == self.selected:
            return
        old, self.selected = self.selected, new
        log.warning("realtime source %s -> %s", old, new)
        self.bus.publish(("status",), self.status_frame())
        if new is not None:  # resync every subscribed key from the new source
            for key in self.bus.subscribed_keys():
                if key[0] in {"ticker", "kline"}:
                    f = self.snapshot_frame(key)
                    if f:
                        self.bus.publish(key, f)

    async def _selector(self) -> None:
        while True:
            try:
                self.reselect()
            except Exception:
                log.exception("realtime selector")
            await asyncio.sleep(1.0)

    # ---- REST cross-check -------------------------------------------------------------------
    def compare(self, ex: str, rest: dict, now: Optional[int] = None) -> dict:
        now = self.clock() if now is None else now
        worst, worst_sym, n = 0.0, None, 0
        for base, rp in rest.items():
            t = self.bus.ticker(ex, base)
            if not t or not rp or now - int(t["at"]) > 5_000:
                continue
            dev = abs(float(t["last"]) - float(rp)) / float(rp) * 1e4
            n += 1
            if dev > worst:
                worst, worst_sym = dev, base
        bad = n > 0 and worst > self.cfg.xcheck_bps
        strikes = self._strikes.get(ex, 0) + 1 if bad else 0
        self._strikes[ex] = strikes
        suspect = False
        if strikes >= self.cfg.xcheck_strikes:
            self.bus.mark_suspect(ex, now + int(2 * max(self.cfg.xcheck_sec, 1) * 1000))
            self._strikes[ex] = 0
            suspect = True
            log.warning("realtime %s stream suspect: %s off REST by %.1f bp", ex, worst_sym, worst)
        prev = self.xcheck.get(ex) or {}
        rep = {"at": now, "compared": n, "maxDevBps": round(worst, 2), "worst": worst_sym, "strikes": strikes,
               "suspect": suspect, "checks": int(prev.get("checks", 0)) + 1,
               "mismatches": int(prev.get("mismatches", 0)) + (1 if bad else 0)}
        self.xcheck[ex] = rep
        return rep

    async def _xcheck_loop(self) -> None:
        if not self.rest_fn or self.cfg.xcheck_sec <= 0:
            return
        await asyncio.sleep(min(15.0, self.cfg.xcheck_sec))
        while True:
            for ex in self.cfg.exchanges:
                if not self.bus.fresh(ex, "ticker"):
                    continue
                try:
                    rest = await asyncio.to_thread(self.rest_fn, ex)
                    self.compare(ex, rest or {})
                except Exception as exc:
                    self.xcheck.setdefault(ex, {})["error"] = f"{type(exc).__name__}: {exc}"[:160]
            await asyncio.sleep(self.cfg.xcheck_sec)

    # ---- lifecycle ------------------------------------------------------------------------
    async def run(self) -> None:
        self.running = True
        self.started_at = self.clock()
        self._tasks = [asyncio.create_task(f.run(), name=f"rt-feed-{f.feed_id}") for f in self.feeds]
        self._tasks.append(asyncio.create_task(self._selector(), name="rt-selector"))
        self._tasks.append(asyncio.create_task(self._xcheck_loop(), name="rt-xcheck"))
        try:
            await asyncio.gather(*self._tasks)
        finally:
            self.running = False
            for f in self.feeds:
                f.stop()
            for t in self._tasks:
                t.cancel()
            await asyncio.gather(*self._tasks, return_exceptions=True)
            for st in self.bus.feed_states():
                st.connected = False

    # ---- websocket client accounting ----------------------------------------------------------
    def admit(self, key: str) -> Optional[str]:
        if self.clients >= self.cfg.max_clients:
            self.rejected += 1
            return "WS_FULL"
        if self.per_key.get(key, 0) >= self.cfg.per_client_key:
            self.rejected += 1
            return "WS_PER_CLIENT"
        self.clients += 1
        self.per_key[key] = self.per_key.get(key, 0) + 1
        return None

    def release(self, key: str) -> None:
        self.clients = max(0, self.clients - 1)
        n = self.per_key.get(key, 0) - 1
        if n > 0:
            self.per_key[key] = n
        else:
            self.per_key.pop(key, None)

    def status(self) -> dict[str, Any]:
        now = self.clock()
        return {
            "enabled": True, "running": self.running, "startedAt": self.started_at, "selected": self.selected,
            "push": self.cfg.push, "overlay": self.cfg.overlay, "staleMs": self.cfg.stale_ms,
            "feeds": [s.view(now, self.cfg.stale_ms) for s in self.bus.feed_states()],
            "backoff": {f.feed_id: [round(d, 2) for d in f.delays[-5:]] for f in self.feeds},
            "xcheck": self.xcheck, "clients": self.clients, "rejected": self.rejected, "slowClosed": self.slow_closed,
            "subscribers": self.bus.subscriber_count(), "published": self.bus.published,
        }
