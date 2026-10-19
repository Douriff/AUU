"""Exchange WebSocket market feed + in-memory bus (docs/realtime-ws-plan.md steps 1-2).

Default OFF (``AUU_RT_WS``). While off, :func:`get_manager` is None and every hook below returns None at once,
so ``MainstreamService`` and ``/api/v1/ws`` run their unchanged code paths.

While on (started from the API lifespan, primary and standby alike — it only reads public market data):

* ``overlay_tickers`` / ``overlay_tail`` give the REST read path the stream's prices / forming bars while the
  stream is fresh; when it goes stale (``AUU_RT_STALE_SEC``, default 10 s) they return None and the existing
  REST ticker cache (3 s) / 10 s tail take over again by themselves;
* ``/api/v1/ws`` accepts ``subscribe`` for ``ticker`` / ``kline`` (see ``ws_session``).
"""
from __future__ import annotations

import logging
from typing import Optional

from app.marketdata.realtime.config import RtConfig, load_rt_config, rt_enabled
from app.marketdata.realtime.manager import RealtimeManager

log = logging.getLogger("auu.realtime")

_MANAGER: Optional[RealtimeManager] = None

__all__ = ["RealtimeManager", "RtConfig", "get_manager", "load_rt_config", "rt_enabled", "build_manager",
           "set_manager", "overlay_tickers", "overlay_tail"]


def get_manager() -> Optional[RealtimeManager]:
    return _MANAGER


def set_manager(m: Optional[RealtimeManager]) -> None:
    global _MANAGER
    _MANAGER = m


def _rest_last(svc, ex: str) -> dict:
    """One batched REST ticker request for the cross-check (worker thread; single attempt, no retry sleep)."""
    bases = svc.cfg.all_symbols()
    with svc._od_lock:
        raw = svc._od_fetcher(ex).once("fetch_tickers", [svc.cfg.spot(b) for b in bases]) or {}
    out = {}
    for b in bases:
        t = raw.get(svc.cfg.spot(b)) or {}
        last = t.get("last") or t.get("close")
        if last:
            out[b] = float(last)
    return out


def build_manager(svc, cfg: Optional[RtConfig] = None, **kw) -> RealtimeManager:
    cfg = cfg or load_rt_config()
    exchanges = [e for e in cfg.exchanges if e in svc.cfg.exchanges] or list(cfg.exchanges)
    if tuple(exchanges) != cfg.exchanges:
        from dataclasses import replace

        cfg = replace(cfg, exchanges=tuple(exchanges))
    return RealtimeManager(
        cfg, bases=svc.cfg.all_symbols(), quote=svc.cfg.quote, display=list(svc.cfg.symbols),
        preferred=lambda: svc.active, rest_fn=kw.pop("rest_fn", lambda ex: _rest_last(svc, ex)), **kw)


def _live(m: Optional[RealtimeManager]) -> bool:
    return m is not None and m.running and m.cfg.overlay


def overlay_tickers(ex: str, bases: list[str]) -> Optional[dict]:
    m = _MANAGER
    if not _live(m):
        return None
    return m.bus.ticker_snapshot(ex, bases)


def overlay_tail(ex: str, base: str, tf: str, step_ms: int) -> Optional[dict]:
    m = _MANAGER
    if not _live(m) or tf not in m.cfg.tfs:
        return None
    return m.bus.tail(ex, base, tf, step_ms)
