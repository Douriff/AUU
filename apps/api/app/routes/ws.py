"""WebSocket /api/v1/ws — hello + subscribe candles|book|trades|signals|fills|risk."""
from __future__ import annotations

import asyncio
import json
import os

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.providers import get_provider

router = APIRouter(tags=["ws"])

VALID_CHANNELS = {"candles", "book", "trades", "signals", "fills", "risk"}


@router.websocket("/api/v1/ws")
async def ws_endpoint(websocket: WebSocket):
    await websocket.accept()
    provider = get_provider()
    providers = [os.getenv("DATA_PROVIDER", "mock")]
    await websocket.send_json({"type": "hello", "version": 1, "providers": providers})

    tasks: dict[str, asyncio.Task] = {}
    stop = asyncio.Event()

    async def pump(channel: str, symbol: str, interval: str | None):
        try:
            async for frame in provider.stream(channel, symbol, interval):
                if stop.is_set():
                    break
                await websocket.send_json(frame)
        except asyncio.CancelledError:
            raise
        except Exception:
            # stream ended / client gone
            return

    async def heartbeat():
        try:
            while not stop.is_set():
                await asyncio.sleep(15)
                await websocket.send_json({"type": "ping"})
        except Exception:
            return

    hb = asyncio.create_task(heartbeat())

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
        for t in tasks.values():
            t.cancel()
        await asyncio.gather(hb, *tasks.values(), return_exceptions=True)
