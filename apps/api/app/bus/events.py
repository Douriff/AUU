"""In-process event hub — paper path emits signal/risk/fill/reject/trading_state to WS."""
from __future__ import annotations

import asyncio
from typing import Any, Callable


class EventHub:
    def __init__(self) -> None:
        self._subs: list[asyncio.Queue[dict[str, Any]]] = []
        self._lock = asyncio.Lock()
        self._sync: list[Callable[[dict[str, Any]], None]] = []
        self._sync_ids: set[int] = set()

    def add_sync_listener(self, fn: Callable[[dict[str, Any]], None]) -> None:
        """In-process observer. Failures are swallowed by publish."""
        ident = id(fn)
        if ident in self._sync_ids:
            return
        self._sync_ids.add(ident)
        self._sync.append(fn)

    async def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=256)
        async with self._lock:
            self._subs.append(q)
        return q

    async def unsubscribe(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        async with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    async def publish(self, event: dict[str, Any]) -> None:
        async with self._lock:
            targets = list(self._subs)
        for q in targets:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                try:
                    q.put_nowait(event)
                except asyncio.QueueFull:
                    pass
        for fn in list(self._sync):
            try:
                fn(event)
            except Exception:
                pass

    def publish_sync(self, event: dict[str, Any]) -> None:
        """Fire-and-forget from sync route handlers."""
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(self.publish(event))
            else:
                loop.run_until_complete(self.publish(event))
        except RuntimeError:
            # no loop yet — drop (WS not connected)
            pass


_hub: EventHub | None = None


def get_hub() -> EventHub:
    global _hub
    if _hub is None:
        _hub = EventHub()
    return _hub
