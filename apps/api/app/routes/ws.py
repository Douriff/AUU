"""WebSocket /api/v1/ws — hello + subscribe + paper event hub fan-out.

With ``AUU_RT_WS=on`` (mainstream mode, default off) it also streams exchange tickers / forming bars from the
in-memory market bus (``app.marketdata.realtime.ws_session``). With the switch off ``rt`` is None and the code
path is the one that ran before: same hello, same replies, no extra limits.
"""
from __future__ import annotations

import asyncio
import json
import os

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.bus import get_hub
from app.legacy import legacy_pump_enabled
from app.providers import AVAILABLE_PROVIDERS, get_provider

router = APIRouter(tags=["ws"])

VALID_CHANNELS = {"candles", "book", "trades", "signals", "fills", "risk"}
# Event types emitted on the wire (hub + provider):
# signal | risk | fill | reject | trading_state | candle | book | trade | …


@router.websocket("/api/v1/ws")
async def ws_endpoint(websocket: WebSocket):
    await websocket.accept()
    flag = getattr(websocket.app.state, "legacy_pump", None)
    legacy = legacy_pump_enabled() if flag is None else bool(flag)
    rt = None if legacy else _rt_session(websocket)
    if rt is not None:
        code = rt.admit()
        if code is not None:
            await websocket.send_json({"type": "error", "payload": {"code": code, "message": "too many market-data connections"}})
            await websocket.close(code=1013, reason=code)
            return
    if legacy:
        provider = get_provider()
        active = os.getenv("DATA_PROVIDER", "mock").lower().strip()
        if active not in AVAILABLE_PROVIDERS:
            active = provider.name
        providers = [active] + [p for p in AVAILABLE_PROVIDERS if p != active]
        hello = {
            "type": "hello",
            "version": 1,
            "providers": providers,
            "orderMode": "paper",
            "venue": "Pump.fun" if provider.name in {"pumpfun_paper", "pumpfun_live_paper"} else "mock",
            "marketData": "real"
            if provider.name == "pumpfun_live_paper"
            else ("synthetic" if provider.name == "pumpfun_paper" else "mock"),
            "eventTypes": ["signal", "risk", "fill", "reject", "trading_state", "pumpfun_curve", "new_token"],
        }
    else:
        # Mainstream mode: no pump provider; only the paper event hub streams.
        provider = None
        hello = {
            "type": "hello",
            "version": 1,
            "providers": ["cex_public"],
            "orderMode": "paper",
            "venue": "CEX",
            "marketData": "real",
            "legacyPump": False,
            "eventTypes": ["signal", "risk", "fill", "reject", "trading_state"],
        }
        if rt is not None:
            hello["eventTypes"] = hello["eventTypes"] + ["ticker", "kline", "rt_status"]
            hello.update(rt.hello_extra())
    if rt is None:
        await websocket.send_json(hello)
    else:
        try:
            await websocket.send_json(hello)
            await websocket.send_text(rt.m.status_frame())
        except BaseException:
            rt.close()
            raise

    tasks: dict[str, asyncio.Task] = {}
    stop = asyncio.Event()
    hub = get_hub()
    hub_q = await hub.subscribe()

    async def pump(channel: str, symbol: str, interval: str | None):
        try:
            async for frame in provider.stream(channel, symbol, interval):
                if stop.is_set():
                    break
                await websocket.send_json(frame)
        except asyncio.CancelledError:
            raise
        except Exception:
            return

    async def hub_pump():
        """Forward paper-path events (fill/reject/risk/trading_state/signal)."""
        try:
            while not stop.is_set():
                try:
                    event = await asyncio.wait_for(hub_q.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                await websocket.send_json(event)
        except asyncio.CancelledError:
            raise
        except Exception:
            return

    async def heartbeat():
        try:
            while not stop.is_set():
                await asyncio.sleep(15)
                await websocket.send_json({"type": "ping"})
        except Exception:
            return

    hb = asyncio.create_task(heartbeat())
    hub_task = asyncio.create_task(hub_pump())
    rt_task = asyncio.create_task(rt.sender()) if rt is not None else None

    try:
        while True:
            if rt is None:
                raw = await websocket.receive_text()
            else:
                try:
                    raw = await asyncio.wait_for(websocket.receive_text(), rt.m.cfg.client_idle_sec)
                except asyncio.TimeoutError:
                    await websocket.close(code=1001, reason="IDLE")
                    break
                if rt_task.done():
                    break  # the sender closed a slow client
                if not rt.allow_message():
                    if rt.violations >= 20:
                        await websocket.close(code=1008, reason="RATE_LIMIT")
                        break
                    await websocket.send_json({"type": "error", "payload": {"code": "RATE_LIMIT", "message": "too many messages"}})
                    continue
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await websocket.send_json(
                    {"type": "error", "payload": {"code": "BAD_JSON", "message": "invalid json"}}
                )
                continue

            mtype = msg.get("type")
            if rt is not None and rt.handles(msg):
                for frame in rt.apply(msg):
                    await websocket.send_text(frame)
                continue
            if mtype == "pong":
                continue
            if mtype == "ping":
                await websocket.send_json({"type": "pong"})
                continue
            if mtype == "unsubscribe":
                key = f"{msg.get('channel')}:{msg.get('symbol')}:{msg.get('interval')}"
                t = tasks.pop(key, None)
                if t:
                    t.cancel()
                continue
            if mtype != "subscribe":
                await websocket.send_json(
                    {"type": "error", "payload": {"code": "UNKNOWN_TYPE", "message": str(mtype)}}
                )
                continue

            channel = msg.get("channel")
            symbol = msg.get("symbol")
            interval = msg.get("interval")
            if provider is None:
                await websocket.send_json(
                    {
                        "type": "error",
                        "payload": {"code": "LEGACY_OFF", "message": "provider channels need AUU_LEGACY_PUMP=on"},
                    }
                )
                continue
            if channel not in VALID_CHANNELS or not symbol:
                await websocket.send_json(
                    {
                        "type": "error",
                        "payload": {
                            "code": "BAD_SUBSCRIBE",
                            "message": "need channel + symbol",
                        },
                    }
                )
                continue

            key = f"{channel}:{symbol}:{interval}"
            if key in tasks:
                tasks[key].cancel()
            tasks[key] = asyncio.create_task(pump(channel, symbol, interval))
            await websocket.send_json(
                {
                    "type": "subscribed",
                    "channel": channel,
                    "symbol": symbol,
                    "interval": interval,
                }
            )
    except WebSocketDisconnect:
        pass
    finally:
        stop.set()
        hb.cancel()
        hub_task.cancel()
        for t in tasks.values():
            t.cancel()
        extra = []
        if rt is not None:
            rt.close()
            rt_task.cancel()
            extra.append(rt_task)
        await hub.unsubscribe(hub_q)
        await asyncio.gather(hb, hub_task, *tasks.values(), *extra, return_exceptions=True)


def _rt_session(websocket: WebSocket):
    """Market-data session when the realtime feed runs with push on; None otherwise (old behaviour)."""
    from app.marketdata.realtime import get_manager

    m = get_manager()
    if m is None or not m.running or not m.cfg.push:
        return None
    from app.marketdata.realtime.ws_session import RtSession

    uid = websocket.scope.get("auu_user_id")
    client = websocket.scope.get("client")
    key = f"u:{uid}" if uid else f"ip:{client[0] if client else 'local'}"
    return RtSession(websocket, m, key)
