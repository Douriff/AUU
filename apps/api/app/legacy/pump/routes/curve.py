"""GET /api/v1/curve?symbol= — Pump.fun bonding-curve snapshot (paper/mock)."""
from __future__ import annotations

from fastapi import APIRouter, Query

from app.providers import get_provider
from app.routes.envelope import ok

router = APIRouter(prefix="/api/v1", tags=["curve"])


@router.get("/curve")
def get_curve(symbol: str = Query(...)):
    """Alias of pumpfun snapshot in UI-friendly shape. Mock returns empty curve fields."""
    provider = get_provider()
    return ok(provider.snapshot_curve(symbol))
