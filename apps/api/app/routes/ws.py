"""WebSocket /api/v1/ws — hello + subscribe + paper event hub fan-out."""
from __future__ import annotations

import asyncio
import json
import os

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.bus import get_hub
from app.providers import AVAILABLE_PROVIDERS, get_provider

router = APIRouter(tags=["ws"])

VALID_CHANNELS = {"candles", "book", "trades", "signals", "fills", "risk"}
# Event types emitted on the wire (hub + provider):
# signal | risk | fill | reject | trading_state | candle | book | trade | …


@router.websocket("/api/v1/ws")
async def ws_endpoint(websocket: WebSocket):
    await websocket.accept()
    provider = get_provider()
    active = os.getenv("DATA_PROVIDER", "mock").lower().strip()
    if active not in AVAILABLE_PROVIDERS:
        active = provider.name
    providers = [active] + [p for p in AVAILABLE_PROVIDERS if p != active]
    await websocket.send_json(
        {
            "type": "hello",
            "version": 1,
            "providers": providers,
            "orderMode": "paper",
            "venue": "Pump.fun" if provider.name == "pumpfun_paper" else "mock",
            "eventTypes": [
                "signal",
                "risk",
                "fill",
                "reject",
                "trading_state",
                "pumpfun_curve",
                "new_token",
            ],
        }
    )

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

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await websocket.send_json(
                    {"type": "error", "payload": {"code": "BAD_JSON", "message": "invalid json"}}
                )
                continue

            mtype = msg.get("type")
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
        await hub.unsubscribe(hub_q)
        await asyncio.gather(hb, hub_task, *tasks.values(), return_exceptions=True)
