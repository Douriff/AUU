"""GET /api/v1/pumpfun/snapshot — paper curve snapshot (no chain)."""
from __future__ import annotations

from fastapi import APIRouter, Query

from app.providers import get_provider
from app.routes.envelope import err, ok
from app.strategies.pump_paper_v1 import get_engine

router = APIRouter(prefix="/api/v1", tags=["pumpfun"])


@router.get("/pumpfun/snapshot")
def pumpfun_snapshot(symbol: str = Query(...)):
    provider = get_provider()
    snap = provider.get_pumpfun_snapshot(symbol)
    if snap is None:
        return err("NO_PUMP_SNAPSHOT", f"no pumpfun snapshot for {symbol}", 404)
    return ok(snap.model_dump())


@router.get("/pumpfun/monitor")
def pumpfun_monitor():
    """Watchlist rows: progress, 1m tape, impact, tags (paper)."""
    return ok(get_engine().monitor_rows())
