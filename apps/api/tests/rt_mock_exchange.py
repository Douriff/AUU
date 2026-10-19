"""Local mock of the Binance spot combined stream and the OKX v5 public/business WebSocket (loopback only).

Used by tests/test_realtime_ws.py and ops/measure/realtime_ws_e2e.py; never talks to a real exchange.
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Optional
from urllib.parse import parse_qs, urlsplit

from websockets.asyncio.server import serve


class MockExchange:
    def __init__(self, kind: str = "binance") -> None:
        self.kind = kind
        self.conns: set = set()
        self.attempts = 0
        self.refuse = 0  # close the next N connections right after the handshake
        self.silent = False  # stop answering "ping" (OKX) — data pushes are controlled by the caller
        self.paths: list[str] = []
        self.received: list[str] = []
        self.pings = 0
        self.streams: list[str] = []
        self.subscribed: list[dict] = []
        self.connected = asyncio.Event()
        self.server = None
        self.port = 0

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}"

    async def start(self) -> "MockExchange":
        # ping_interval=None: the mock never pings; the client's own protocol pings are answered automatically.
        self.server = await serve(self._handler, "127.0.0.1", 0, ping_interval=None, compression=None)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def stop(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()

    async def _handler(self, ws) -> None:
        self.attempts += 1
        path = ws.request.path
        self.paths.append(path)
        if self.refuse > 0:
            self.refuse -= 1
            await ws.close(1011, "refused")
            return
        if self.kind == "binance":
            q = parse_qs(urlsplit(path).query)
            self.streams = (q.get("streams") or [""])[0].split("/")
        self.conns.add(ws)
        self.connected.set()
        try:
            async for msg in ws:
                self.received.append(msg)
                if msg == "ping":
                    self.pings += 1
                    if not self.silent:
                        await ws.send("pong")
                    continue
                if self.kind == "okx":
                    m = json.loads(msg)
                    if m.get("op") == "subscribe":
                        self.subscribed.extend(m.get("args") or [])
                        for a in m.get("args") or []:
                            await ws.send(json.dumps({"event": "subscribe", "arg": a, "connId": "mock"}))
        except Exception:
            pass
        finally:
            self.conns.discard(ws)
            if not self.conns:
                self.connected.clear()

    async def broadcast(self, frame: str) -> int:
        n = 0
        for ws in list(self.conns):
            try:
                await ws.send(frame)
                n += 1
            except Exception:
                pass
        return n

    async def drop_all(self) -> None:
        for ws in list(self.conns):
            await ws.close(1001, "going away")

    # ---- frame builders ----------------------------------------------------------------------
    @staticmethod
    def binance_ticker(base: str, last: float, open_: Optional[float] = None, qv: float = 1e9,
                       event_ms: Optional[int] = None) -> str:
        e = int(time.time() * 1000) if event_ms is None else event_ms
        s = f"{base}USDT"
        return json.dumps({"stream": f"{s.lower()}@miniTicker", "data": {
            "e": "24hrMiniTicker", "E": e, "s": s, "c": f"{last}", "o": f"{open_ or last}", "h": f"{last}",
            "l": f"{last}", "v": "1000", "q": f"{qv}"}})

    @staticmethod
    def binance_kline(base: str, tf: str, t: int, o: float, h: float, l: float, c: float, v: float = 10.0,
                      closed: bool = False, event_ms: Optional[int] = None) -> str:
        e = int(time.time() * 1000) if event_ms is None else event_ms
        s = f"{base}USDT"
        return json.dumps({"stream": f"{s.lower()}@kline_{tf}", "data": {
            "e": "kline", "E": e, "s": s, "k": {"t": t, "T": t + 1, "s": s, "i": tf, "o": f"{o}", "c": f"{c}",
                                               "h": f"{h}", "l": f"{l}", "v": f"{v}", "x": closed}}})

    @staticmethod
    def okx_ticker(base: str, last: float, open_: Optional[float] = None, qv: float = 1e9) -> str:
        return json.dumps({"arg": {"channel": "tickers", "instId": f"{base}-USDT"}, "data": [{
            "instId": f"{base}-USDT", "last": f"{last}", "open24h": f"{open_ or last}", "volCcy24h": f"{qv}",
            "ts": str(int(time.time() * 1000))}]})

    @staticmethod
    def okx_candle(base: str, channel: str, t: int, o: float, h: float, l: float, c: float, confirm: str = "0") -> str:
        return json.dumps({"arg": {"channel": channel, "instId": f"{base}-USDT"},
                           "data": [[str(t), f"{o}", f"{h}", f"{l}", f"{c}", "10", "10", "1000", confirm]]})
