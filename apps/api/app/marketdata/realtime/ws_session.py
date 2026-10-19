"""Market-data part of ``/api/v1/ws`` (only used while ``AUU_RT_WS`` is on and push is enabled).

Protocol (JSON text frames, additions only — the paper event hub frames are unchanged):

* client -> ``{"type":"subscribe","channel":"ticker","symbol":"BTC"}`` (or ``"symbols":["BTC","ETH"]``);
  ``{"type":"subscribe","channel":"kline","symbol":"BTC","interval":"1h"}`` (1h/4h/1d);
  ``unsubscribe`` with the same fields;
* server -> ``subscribed`` ack, then the latest snapshot, then every update: ``ticker`` / ``kline`` frames
  (``recvAt`` = when this server got it from the exchange, ``ts``/``eventTs`` = exchange time), and
  ``rt_status`` (``live``/``source``) whenever the source exchange changes or the stream goes stale — the client
  then falls back to REST polling (step 3).

Limits: per-process and per-account (IP when anonymous) connection caps, subscription cap, inbound message rate
(token bucket per 10 s), idle timeout (no client frame for ``client_idle_sec``; browsers answer the 15 s ping),
and a send timeout: a client that cannot take a frame within ``send_timeout_sec`` is closed (1013) instead of
buffering. Updates per key are coalesced (latest wins) so a slow client costs at most one frame per key.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Optional

from app.marketdata.realtime.bus import Subscriber, dumps
from app.marketdata.realtime.manager import RealtimeManager

CHANNELS = ("ticker", "kline")


def _base(raw: Any) -> str:
    s = str(raw or "").split("/")[0].split("-")[0].upper()
    s = "".join(ch for ch in s if ch.isalnum())
    for q in ("USDT",):
        if s.endswith(q) and len(s) > len(q):
            s = s[: -len(q)]
    return s


def _err(code: str, message: str) -> str:
    return dumps({"type": "error", "payload": {"code": code, "message": message}})


class RtSession:
    def __init__(self, websocket, manager: RealtimeManager, client_key: str) -> None:
        self.ws, self.m, self.key = websocket, manager, client_key
        self.sub = Subscriber({"key": client_key})
        self.admitted = False
        self._tokens = float(manager.cfg.msg_burst)
        self._tok_at = time.monotonic()
        self.violations = 0
        self.closed_reason: Optional[str] = None

    # ---- lifecycle -------------------------------------------------------------------------
    def admit(self) -> Optional[str]:
        code = self.m.admit(self.key)
        self.admitted = code is None
        if self.admitted:
            self.m.bus.add(self.sub, ("status",))
        return code

    def close(self) -> None:
        self.m.bus.drop(self.sub)
        if self.admitted:
            self.m.release(self.key)
            self.admitted = False

    def hello_extra(self) -> dict:
        return {"realtime": {"channels": list(CHANNELS), "intervals": list(self.m.cfg.tfs), "symbols": list(self.m.bases),
                             "maxSubs": self.m.cfg.max_subs, "staleMs": self.m.cfg.stale_ms}}

    # ---- inbound ---------------------------------------------------------------------------
    def allow_message(self) -> bool:
        """Token bucket: ``msg_burst`` messages per 10 s."""
        now = time.monotonic()
        rate = self.m.cfg.msg_burst / 10.0
        self._tokens = min(float(self.m.cfg.msg_burst), self._tokens + (now - self._tok_at) * rate)
        self._tok_at = now
        if self._tokens >= 1.0:
            self._tokens -= 1.0
            return True
        self.violations += 1
        return False

    def handles(self, msg: dict) -> bool:
        return msg.get("type") in {"subscribe", "unsubscribe"} and msg.get("channel") in CHANNELS

    def apply(self, msg: dict) -> list[str]:
        """Handle a ticker/kline (un)subscribe; returns frames to send (acks, errors, snapshots)."""
        channel = msg.get("channel")
        raw_syms = msg.get("symbols") if isinstance(msg.get("symbols"), list) else [msg.get("symbol")]
        bases = [_base(s) for s in raw_syms[:64] if s]
        interval = msg.get("interval")
        if not bases or any(b not in self.m.bases for b in bases):
            return [_err("BAD_SUBSCRIBE", "unknown symbol")]
        if channel == "kline" and interval not in self.m.cfg.tfs:
            return [_err("BAD_SUBSCRIBE", f"interval must be one of {','.join(self.m.cfg.tfs)}")]
        keys = [("ticker", b) if channel == "ticker" else ("kline", b, interval) for b in bases]
        out: list[str] = []
        if msg.get("type") == "unsubscribe":
            for k in keys:
                self.m.bus.discard(self.sub, k)
            return [dumps({"type": "unsubscribed", "channel": channel, "symbols": bases, "interval": interval})]
        new = [k for k in keys if k not in self.sub.keys]
        if len(self.sub.keys) - 1 + len(new) > self.m.cfg.max_subs:  # minus the implicit status key
            return [_err("TOO_MANY_SUBS", f"max {self.m.cfg.max_subs} subscriptions per connection")]
        for k in new:
            self.m.bus.add(self.sub, k)
        out.append(dumps({"type": "subscribed", "channel": channel, "symbol": bases[0], "symbols": bases,
                          "interval": interval}))
        out.append(self.m.status_frame())
        for k in keys:
            f = self.m.snapshot_frame(k)
            if f:
                out.append(f)
        return out

    # ---- outbound --------------------------------------------------------------------------
    async def sender(self) -> None:
        """Single writer for market frames; closes a client that cannot keep up."""
        timeout = self.m.cfg.send_timeout_sec
        while True:
            await self.sub.event.wait()
            self.sub.event.clear()
            for frame in self.sub.drain():
                try:
                    await asyncio.wait_for(self.ws.send_text(frame), timeout)
                except asyncio.TimeoutError:
                    self.m.slow_closed += 1
                    self.closed_reason = "SLOW_CLIENT"
                    try:
                        await asyncio.wait_for(self.ws.close(code=1013, reason="SLOW_CLIENT"), 1.0)
                    except Exception:
                        pass
                    return
                self.sub.sent += 1
