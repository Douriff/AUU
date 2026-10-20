"""Exchange WebSocket clients (public market data only): Binance spot combined stream, OKX public/business.

Each feed is one asyncio task holding one connection:

* connect with a timeout, subscribe, then read; every frame is parsed inline (tiny JSON, ~10 µs) and handed to
  the sink — no thread hops, no sqlite, nothing that can block the event loop;
* liveness: protocol pings (``websockets`` answers Binance's 20 s server pings automatically and sends its own),
  an idle watchdog (no message for ``idle_sec`` -> reconnect; OKX first gets the text ``ping`` it expects), and a
  rotation before Binance's 24 h hard cut;
* reconnect with exponential backoff + jitter (``base * 2^(n-1)``, capped, ×[0.5, 1)), reset once a connection
  has delivered data for ``stable_sec``; plus a per-feed budget of connection attempts per 5 minutes (Binance
  allows 300/IP, OKX 3/s/IP), and the URL list is rotated after a failure.

OKX standby (``AUU_RT_OKX_MODE=standby``, the default when Binance is configured too): the manager flips the two
OKX feeds with :meth:`BaseFeed.set_active`. The ``/public`` (tickers) connection stays open with only the probe
subscription and switches ``tickers`` on/off with one subscribe/unsubscribe op on the live connection (warm: no
new handshake when Binance fails). The ``/business`` (candles) feed is lazy: no connection in standby, it connects
when activated and closes again on standby (charts use the REST tail meanwhile).
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from collections import deque
from typing import Any, Callable, Optional, Protocol

from app.marketdata.realtime.bus import FeedState, MarketBus, now_ms
from app.marketdata.realtime.config import RtConfig

log = logging.getLogger("auu.realtime")

BINANCE_TF = {"1h": "1h", "4h": "4h", "1d": "1d"}
BINANCE_TF_BACK = {v: k for k, v in BINANCE_TF.items()}
# OKX: 1D/2D/... are aligned to Hong Kong time; the ``utc`` variants match Binance/our store (UTC). 1H/4H are
# the same either way (8 h offset).
OKX_TF = {"1h": "candle1H", "4h": "candle4H", "1d": "candle1Dutc"}
OKX_TF_BACK = {v: k for k, v in OKX_TF.items()}


class Sink(Protocol):
    def on_ticker(self, ex: str, base: str, row: dict) -> None: ...
    def on_bar(self, ex: str, base: str, tf: str, row: list, closed: bool, event_ts: Optional[int], recv: int) -> None: ...


class FeedIdle(Exception):
    pass


def _f(x: Any) -> Optional[float]:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v


class BaseFeed:
    ex = "?"
    feed_id = "?"
    carries: tuple[str, ...] = ()

    def __init__(self, cfg: RtConfig, bases: list[str], quote: str, bus: MarketBus, sink: Sink, *,
                 connect: Optional[Callable[..., Any]] = None, sleep: Callable[[float], Any] = asyncio.sleep,
                 rand: Callable[[], float] = random.random, clock: Callable[[], int] = now_ms,
                 monotonic: Callable[[], float] = time.monotonic) -> None:
        self.cfg, self.bases, self.quote, self.bus, self.sink = cfg, list(bases), quote.upper(), bus, sink
        if connect is None:
            from websockets.asyncio.client import connect as ws_connect

            connect = ws_connect
        self._connect = connect
        self._sleep, self._rand, self._clock, self._mono = sleep, rand, clock, monotonic
        self.state: FeedState = bus.feed(self.feed_id, self.ex, self.carries)
        self._stop = False
        self._attempt = 0
        self._url_idx = 0
        self._recent: deque[float] = deque()
        self.delays: list[float] = []  # backoff history (tests / status)
        # standby support (see module doc). ``active`` is what the manager wants; plain feeds are always active.
        self.active = True
        self.lazy = False  # True: no connection at all while not active
        self._ws = None
        self._wake: Optional[asyncio.Event] = None
        self._mode_lock: Optional[asyncio.Lock] = None
        self.on_down: Optional[Callable[[str], None]] = None  # manager hook: connection lost

    # ---- per exchange ---------------------------------------------------------------------
    def urls(self) -> list[str]:
        raise NotImplementedError

    async def on_open(self, ws) -> None:  # subscribe
        return None

    async def on_quiet(self, ws, pinged: bool) -> bool:
        """Called after ``idle_sec`` without a message. Return True to keep waiting once more."""
        return False

    def handle(self, raw: Any, recv: int) -> None:
        raise NotImplementedError

    async def sync_mode(self, ws) -> None:
        """Bring the live connection's subscriptions in line with ``self.active`` (OKX tickers only)."""
        return None

    # ---- standby ----------------------------------------------------------------------------
    def _event(self) -> asyncio.Event:
        if self._wake is None:
            self._wake = asyncio.Event()
        return self._wake

    async def set_active(self, on: bool) -> None:
        """Called by the manager (event loop). Never blocks on the network beyond one small send / close."""
        on = bool(on)
        if on == self.active:
            return
        self.active = on
        self.state.mode = "active" if on else "standby"
        if on:
            self.state.since_ms = self._clock()  # data from before the standby period never counts
            self._event().set()
        ws = self._ws
        if ws is None:
            return
        if self.lazy and not on:
            try:
                await ws.close()
            except Exception:
                pass
            return
        try:
            await self.sync_mode(ws)
        except Exception as exc:  # the connection is going away; on_open re-subscribes per ``active``
            self.state.last_error = f"mode: {type(exc).__name__}: {exc}"[:200]

    def _park(self) -> None:  # start in standby; the manager wakes the feed when Binance needs a backup
        self.active = False
        self.state.mode = "standby"

    def _standby_parked(self) -> bool:
        return self.lazy and not self.active

    # ---- loop -----------------------------------------------------------------------------
    def stop(self) -> None:
        self._stop = True
        if self._wake is not None:
            self._wake.set()

    def next_delay(self) -> float:
        base, cap = self.cfg.backoff_base_sec, self.cfg.backoff_max_sec
        n = max(1, self._attempt)
        raw = min(cap, base * (2 ** (n - 1)))
        return raw * (0.5 + 0.5 * self._rand())

    async def _connect_budget(self) -> None:
        window, cap = 300.0, max(1, self.cfg.max_connects_5min)
        now = self._mono()
        while self._recent and now - self._recent[0] >= window:
            self._recent.popleft()
        if len(self._recent) >= cap:
            wait = window - (now - self._recent[0])
            log.warning("realtime %s: %d connects in 5 min, waiting %.0fs", self.feed_id, cap, wait)
            await self._sleep(max(0.0, wait))
        self._recent.append(self._mono())

    async def run(self) -> None:
        while not self._stop:
            if self._standby_parked():
                ev = self._event()
                ev.clear()
                if self._standby_parked() and not self._stop:
                    await ev.wait()
                continue
            await self._connect_budget()
            urls = self.urls()
            url = urls[self._url_idx % len(urls)]
            started = self._mono()
            got_data_before = self.state.last_data_ms
            err: Optional[str] = None
            rotated = False
            try:
                async with self._connect(url, open_timeout=self.cfg.open_timeout_sec, ping_interval=self.cfg.ws_ping_sec,
                                         ping_timeout=self.cfg.ws_ping_sec, close_timeout=2, max_size=2 ** 20,
                                         max_queue=1024, compression=None) as ws:
                    st = self.state
                    st.connected, st.url, st.connected_at = True, url, self._clock()
                    st.rotating = False
                    st.connects += 1
                    log.warning("realtime %s connected %s", self.feed_id, url)
                    await self.on_open(ws)
                    self._ws = ws
                    await self.sync_mode(ws)  # ``active`` may have changed while on_open was sending
                    if not self._standby_parked():
                        rotated = await self._read(ws, started)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # network, protocol, idle watchdog
                err = f"{type(exc).__name__}: {exc}"[:200]
            finally:
                self._ws = None
                if self.state.connected:
                    self.state.disconnects += 1
                self.state.connected = False
                if err and not self._standby_parked():
                    self.state.last_error = err
            if self._stop:
                break
            if self._standby_parked():  # closed on purpose by set_active(False): no backoff, no failure
                self._attempt = 0
                log.warning("realtime %s standby (connection closed)", self.feed_id)
                continue
            if self.on_down is not None and not rotated:
                try:
                    self.on_down(self.feed_id)
                except Exception:
                    log.exception("realtime %s on_down", self.feed_id)
            delivered = self.state.last_data_ms != got_data_before
            if rotated or (delivered and self._mono() - started >= self.cfg.stable_sec):
                self._attempt = 0
                delay = self.cfg.backoff_base_sec * (0.5 + 0.5 * self._rand())
            else:
                self._attempt += 1
                if not delivered:
                    self._url_idx += 1  # next gateway after a connection that never delivered
                delay = self.next_delay()
            self.delays.append(delay)
            if len(self.delays) > 50:
                del self.delays[:-50]
            log.warning("realtime %s disconnected (%s); reconnect in %.1fs", self.feed_id, err or "closed", delay)
            await self._sleep(delay)

    async def _read(self, ws, started: float) -> bool:
        """Read until error/idle. Returns True when the connection was rotated on purpose (max age)."""
        pinged = False
        while not self._stop and not self._standby_parked():
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=self.cfg.idle_sec)
            except asyncio.TimeoutError:
                if await self.on_quiet(ws, pinged):
                    pinged = True
                    continue
                raise FeedIdle(f"no message for {self.cfg.idle_sec:.0f}s")
            pinged = False
            recv = self._clock()
            st = self.state
            st.last_msg_ms = recv
            st.messages += 1
            try:
                self.handle(raw, recv)
            except Exception as exc:  # one bad frame never kills the feed
                st.parse_errors += 1
                if st.parse_errors <= 5 or st.parse_errors % 1000 == 0:
                    log.warning("realtime %s bad frame (%s): %r", self.feed_id, exc, str(raw)[:160])
            if st.messages % 50 == 0:
                await asyncio.sleep(0)  # a queued burst never monopolises the loop
            if self._mono() - started >= self.cfg.max_conn_age_sec:
                self.state.rotating = True  # planned: the OKX controller waits for real staleness
                await ws.close()
                return True
        return False

    # ---- helpers --------------------------------------------------------------------------
    def _data(self, recv: int) -> None:
        self.state.last_data_ms = recv


class BinanceSpotFeed(BaseFeed):
    """One combined-stream connection: ``<sym>@miniTicker`` (1 s) + ``<sym>@kline_<tf>`` (2 s) for every coin."""

    ex = "binance"
    feed_id = "binance"
    carries = ("ticker", "kline")

    def __init__(self, *a, **kw) -> None:
        super().__init__(*a, **kw)
        self._sym = {f"{b}{self.quote}".lower(): b for b in self.bases}

    def streams(self) -> list[str]:
        out = []
        for s in self._sym:
            out.append(f"{s}@miniTicker")
            out.extend(f"{s}@kline_{BINANCE_TF[tf]}" for tf in self.cfg.tfs)
        return out  # 19 coins x 4 = 76 streams (limit 1024 per connection)

    def urls(self) -> list[str]:
        q = "/".join(self.streams())
        return [f"{u}/stream?streams={q}" for u in self.cfg.binance_urls]

    def handle(self, raw: Any, recv: int) -> None:
        msg = json.loads(raw)
        d = msg.get("data") if isinstance(msg, dict) else None
        if not isinstance(d, dict):
            return  # e.g. {"result":null,"id":1}
        e = d.get("e")
        base = self._sym.get(str(d.get("s", "")).lower())
        if base is None:
            return
        if e == "24hrMiniTicker":
            last, op = _f(d.get("c")), _f(d.get("o"))
            if not last:
                return
            self._data(recv)
            self.sink.on_ticker(self.ex, base, {
                "last": last, "change24h": (last / op - 1) if op else None, "quoteVolume": _f(d.get("q")),
                "ts": int(d["E"]) if d.get("E") else None, "at": recv})
        elif e == "kline":
            k = d.get("k") or {}
            tf = BINANCE_TF_BACK.get(k.get("i"))
            if tf is None or k.get("t") is None:
                return
            row = [int(k["t"]), _f(k.get("o")), _f(k.get("h")), _f(k.get("l")), _f(k.get("c")), _f(k.get("v"))]
            if row[4] is None:
                return
            self._data(recv)
            self.sink.on_bar(self.ex, base, tf, row, bool(k.get("x")), int(d["E"]) if d.get("E") else None, recv)


class _OkxBase(BaseFeed):
    ex = "okx"

    def __init__(self, *a, **kw) -> None:
        super().__init__(*a, **kw)
        self._inst = {f"{b}-{self.quote}": b for b in self.bases}
        self._ops: deque[float] = deque()  # subscribe/unsubscribe ops on the current connection (last hour)
        self.ops_refused = 0

    def args(self) -> list[dict]:
        raise NotImplementedError

    def _op_allowed(self) -> bool:
        now = self._mono()
        while self._ops and now - self._ops[0] >= 3600.0:
            self._ops.popleft()
        if len(self._ops) >= max(1, self.cfg.okx_ops_per_hour):
            self.ops_refused += 1
            log.warning("realtime %s: %d subscribe ops in the last hour, op skipped", self.feed_id, len(self._ops))
            return False
        self._ops.append(now)
        return True

    async def _op(self, ws, op: str, args: list[dict]) -> bool:
        if not args or not self._op_allowed():
            return False
        await ws.send(json.dumps({"id": f"auu{len(self._ops)}", "op": op, "args": args}, separators=(",", ":")))
        return True

    async def on_open(self, ws) -> None:
        # one subscribe op for every channel (limit 480 ops/hour/connection, 64 KB per message)
        self._ops.clear()  # the limit is per connection
        await self._op(ws, "subscribe", self.args())

    async def on_quiet(self, ws, pinged: bool) -> bool:
        if pinged:
            return False
        await ws.send("ping")  # OKX drops a connection after 30 s without traffic
        return True

    def handle(self, raw: Any, recv: int) -> None:
        if raw == "pong":
            return
        msg = json.loads(raw)
        if not isinstance(msg, dict):
            return
        if msg.get("event"):
            if msg["event"] in ("error", "channel-conn-count-error"):
                self.state.last_error = f"okx {msg.get('code')}: {str(msg.get('msg'))[:120]}"
                log.warning("realtime %s %s", self.feed_id, self.state.last_error)
            return
        arg, data = msg.get("arg") or {}, msg.get("data")
        base = self._inst.get(str(arg.get("instId", "")))
        if base is None or not isinstance(data, list):
            return
        self.on_data(str(arg.get("channel", "")), base, data, recv)

    def on_data(self, channel: str, base: str, data: list, recv: int) -> None:
        raise NotImplementedError


class OkxTickerFeed(_OkxBase):
    """``tickers`` for every coin. In standby mode the connection is kept warm with one probe subscription and
    ``tickers`` is (un)subscribed on the live connection when the manager flips :attr:`active`."""

    feed_id = "okx:tickers"
    carries = ("ticker",)

    def __init__(self, *a, **kw) -> None:
        super().__init__(*a, **kw)
        self.standby_mode = okx_standby_applies(self.cfg)
        self._server_has_tickers = False
        if self.standby_mode:
            self._park()

    def urls(self) -> list[str]:
        return [self.cfg.okx_public_url]

    def ticker_args(self) -> list[dict]:
        return [{"channel": "tickers", "instId": i} for i in self._inst]

    def probe_args(self) -> list[dict]:
        ch, inst = self.cfg.okx_probe
        return [{"channel": ch, "instId": inst}]

    def args(self) -> list[dict]:
        if not self.standby_mode:
            return self.ticker_args()
        return self.probe_args() + (self.ticker_args() if self.active else [])

    async def on_open(self, ws) -> None:
        self._server_has_tickers = False
        await super().on_open(ws)
        self._server_has_tickers = (not self.standby_mode) or self.active

    async def sync_mode(self, ws) -> None:
        if not self.standby_mode:
            return
        if self._mode_lock is None:
            self._mode_lock = asyncio.Lock()
        async with self._mode_lock:  # set_active and the post-connect sync never interleave their ops
            want = self.active
            if want == self._server_has_tickers:
                return
            if await self._op(ws, "subscribe" if want else "unsubscribe", self.ticker_args()):
                self._server_has_tickers = want
                log.warning("realtime %s tickers %s", self.feed_id, "subscribed" if want else "unsubscribed (standby)")
            elif want:  # budget exhausted on this connection: a new connection subscribes everything at once
                await ws.close()

    def handle(self, raw: Any, recv: int) -> None:
        if self.standby_mode and raw != "pong" and isinstance(raw, str) and self.cfg.okx_probe[0] in raw:
            try:
                msg = json.loads(raw)
            except ValueError:
                msg = None
            arg = msg.get("arg") if isinstance(msg, dict) else None
            if isinstance(arg, dict) and arg.get("channel") == self.cfg.okx_probe[0] and not msg.get("event"):
                self.state.probe_ms = recv  # liveness only, never market data
                return
        super().handle(raw, recv)

    def on_data(self, channel: str, base: str, data: list, recv: int) -> None:
        if channel != "tickers":
            return
        for d in data:
            last, op = _f(d.get("last")), _f(d.get("open24h"))
            if not last:
                continue
            self._data(recv)
            self.sink.on_ticker(self.ex, base, {
                "last": last, "change24h": (last / op - 1) if op else None, "quoteVolume": _f(d.get("volCcy24h")),
                "ts": int(d["ts"]) if d.get("ts") else None, "at": recv})


class OkxCandleFeed(_OkxBase):
    feed_id = "okx:candles"
    carries = ("kline",)

    def __init__(self, *a, **kw) -> None:
        super().__init__(*a, **kw)
        self.lazy = okx_standby_applies(self.cfg)  # standby: not connected at all
        if self.lazy:
            self._park()

    def urls(self) -> list[str]:
        return [self.cfg.okx_business_url]

    def args(self) -> list[dict]:
        return [{"channel": OKX_TF[tf], "instId": i} for i in self._inst for tf in self.cfg.tfs]

    def on_data(self, channel: str, base: str, data: list, recv: int) -> None:
        tf = OKX_TF_BACK.get(channel)
        if tf is None:
            return
        for r in data:
            if not isinstance(r, list) or len(r) < 5:
                continue
            row = [int(r[0]), _f(r[1]), _f(r[2]), _f(r[3]), _f(r[4]), _f(r[5]) if len(r) > 5 else None]
            if row[4] is None:
                continue
            self._data(recv)
            closed = str(r[8]) == "1" if len(r) > 8 else False
            self.sink.on_bar(self.ex, base, tf, row, closed, None, recv)


def okx_standby_applies(cfg: RtConfig) -> bool:
    """Standby only makes sense when OKX is the backup of Binance; OKX alone always runs full."""
    return cfg.okx_mode == "standby" and "okx" in cfg.exchanges and "binance" in cfg.exchanges


def build_feeds(cfg: RtConfig, bases: list[str], quote: str, bus: MarketBus, sink: Sink, **kw) -> list[BaseFeed]:
    feeds: list[BaseFeed] = []
    for ex in cfg.exchanges:
        if ex == "binance":
            feeds.append(BinanceSpotFeed(cfg, bases, quote, bus, sink, **kw))
        elif ex == "okx":
            feeds.append(OkxTickerFeed(cfg, bases, quote, bus, sink, **kw))
            feeds.append(OkxCandleFeed(cfg, bases, quote, bus, sink, **kw))
    return feeds
