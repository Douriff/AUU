"""Runs the exchange feeds, picks the source exchange, cross-checks against REST and pushes frames to the bus.

Source selection (every second): the exchange the REST layer is using (``MainstreamService.active``, Binance
normally) when its stream is fresh, else the first other fresh stream (OKX), else none -> clients get
``rt_status live=false`` and every reader falls back to REST. Frames are only pushed for the selected
exchange, so a client never sees two venues' prices interleaved.

REST cross-check (every ``xcheck_sec``, in a worker thread, one ``ticker/24hr`` request ~ weight 2): a stream
whose prices differ from REST by more than ``xcheck_bps`` on ``xcheck_strikes`` consecutive checks is marked
suspect for two check periods (readers use REST meanwhile).

OKX standby (``AUU_RT_OKX_MODE=standby``, default; see ``config.RtConfig.okx_mode``): :meth:`okx_control` runs
on every selector pass. OKX is woken (full tickers on the already open ``/public`` connection, candles connect)
as soon as Binance is disconnected, its data is older than ``okx_wake_ms``, it is suspect or has a cross-check
strike, or REST prefers OKX; and it is put back to standby once Binance has been healthy for the calm period.
Every re-activation inside the flap window doubles the calm period (capped), so a flapping Binance keeps OKX hot
instead of toggling subscriptions. A Binance disconnect wakes the selector at once (no 1 s tick wait).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Optional

from app.marketdata.realtime.bus import MarketBus, Subscriber, dumps, now_ms
from app.marketdata.realtime.config import RtConfig
from collections import deque

from app.marketdata.realtime.feeds import BaseFeed, build_feeds, okx_standby_applies

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
        # OKX standby controller (event loop only)
        self.okx_ctl = okx_standby_applies(cfg)
        self.okx_active = not self.okx_ctl
        self.okx_since: Optional[int] = None
        self.okx_reason: Optional[str] = None
        self.okx_activations = 0
        self._okx_wakes: deque[int] = deque()  # activation times inside the flap window
        self._calm_since: Optional[int] = None
        self._mode_tasks: set[asyncio.Task] = set()
        self._kick: Optional[asyncio.Event] = None
        if self.okx_ctl:
            for f in self.feeds:
                f.on_down = self._feed_down
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
        elif self.okx_ctl and self.selected is None:
            self.kick()  # first frames of a freshly woken backup: select it now, not on the next 1 s tick

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

    def kick(self) -> None:
        if self._kick is not None:
            self._kick.set()

    def _feed_down(self, feed_id: str) -> None:
        self.kick()

    async def _selector(self) -> None:
        if self.okx_ctl:
            self._kick = asyncio.Event()
        while True:
            try:
                if self.okx_ctl:
                    self.okx_control()
                self.reselect()
            except Exception:
                log.exception("realtime selector")
            if self._kick is None:
                await asyncio.sleep(1.0)
            else:
                try:
                    await asyncio.wait_for(self._kick.wait(), timeout=1.0)
                except asyncio.TimeoutError:
                    pass
                self._kick.clear()

    # ---- OKX standby controller -----------------------------------------------------------------
    def _primary_state(self):
        for st in self.bus.feed_states():
            if st.ex == "binance" and "ticker" in st.carries:
                return st
        return None

    def okx_need(self, now: int) -> Optional[str]:
        """Why OKX must be fully subscribed right now (None = Binance is healthy)."""
        if self.preferred() == "okx":
            return "rest-prefers-okx"
        st = self._primary_state()
        if st is None:
            return "no-binance-feed"
        if not st.last_data_ms and self.started_at is not None and now - self.started_at < self.cfg.okx_boot_grace_sec * 1000:
            return None  # start-up: Binance has not delivered yet, give it the grace period
        if not st.connected and not st.rotating:  # a planned 24 h rotation only wakes OKX if it gets stale
            return "binance-disconnected"
        if not st.last_data_ms or now - st.last_data_ms > self.cfg.okx_wake_ms:
            return "binance-stale"
        if now < st.suspect_until_ms:
            return "binance-suspect"
        if self._strikes.get("binance", 0) > 0:
            return "binance-xcheck-strike"
        return None

    def okx_calm_ms(self, now: Optional[int] = None) -> int:
        now = self.clock() if now is None else now
        win = int(self.cfg.okx_flap_window_sec * 1000)
        while self._okx_wakes and now - self._okx_wakes[0] > win:
            self._okx_wakes.popleft()
        n = max(1, len(self._okx_wakes))
        return int(min(self.cfg.okx_calm_max_sec, self.cfg.okx_calm_sec * (2 ** (n - 1))) * 1000)

    def okx_control(self, now: Optional[int] = None) -> None:
        if not self.okx_ctl:
            return
        now = self.clock() if now is None else now
        reason = self.okx_need(now)
        if reason is not None:
            self._calm_since = None
            if not self.okx_active:
                self._okx_set(True, now, reason)
            return
        if not self.okx_active:
            return
        if self._calm_since is None:
            self._calm_since = now
            return
        calm = self.okx_calm_ms(now)
        if now - self._calm_since >= calm:
            self._okx_set(False, now, f"binance healthy for {calm / 1000:g}s")

    def _okx_set(self, on: bool, now: int, reason: str) -> None:
        self.okx_active, self.okx_since, self.okx_reason = on, now, reason
        self._calm_since = None
        if on:
            self.okx_activations += 1
            self._okx_wakes.append(now)
        log.warning("realtime okx %s (%s)", "standby -> active" if on else "active -> standby", reason)
        for f in self.feeds:
            if f.ex == "okx":
                t = asyncio.get_running_loop().create_task(f.set_active(on), name=f"rt-mode-{f.feed_id}")
                self._mode_tasks.add(t)
                t.add_done_callback(self._mode_tasks.discard)

    def okx_view(self, now: int) -> dict:
        if not self.okx_ctl:
            return {"mode": "full", "active": True}
        tick = next((f for f in self.feeds if f.feed_id == "okx:tickers"), None)
        st = tick.state if tick else None
        probe_ok = bool(st and st.connected and st.probe_ms and now - st.probe_ms <= self.cfg.okx_probe_stale_sec * 1000)
        return {"mode": "standby", "active": self.okx_active, "since": self.okx_since, "reason": self.okx_reason,
                "activations": self.okx_activations, "calmSec": self.okx_calm_ms(now) // 1000,
                "calmLeftSec": (max(0, self.okx_calm_ms(now) - (now - self._calm_since)) // 1000
                                if self.okx_active and self._calm_since is not None else None),
                "standbyHealthy": probe_ok, "probe": "%s:%s" % self.cfg.okx_probe,
                "opsRefused": getattr(tick, "ops_refused", 0)}

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
            for t in list(self._tasks) + list(self._mode_tasks):
                t.cancel()
            await asyncio.gather(*self._tasks, *self._mode_tasks, return_exceptions=True)
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
            "okx": self.okx_view(now), "xcheck": self.xcheck, "clients": self.clients, "rejected": self.rejected, "slowClosed": self.slow_closed,
            "subscribers": self.bus.subscriber_count(), "published": self.bus.published,
        }
