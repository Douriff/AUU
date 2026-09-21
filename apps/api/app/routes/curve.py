"""GET /api/v1/curve?symbol= — pump.fun bonding-curve snapshot (paper/mock)."""
from __future__ import annotations

from fastapi import APIRouter, Query

from app.providers import get_provider
from app.routes.envelope import ok

router = APIRouter(prefix="/api/v1", tags=["curve"])


@router.get("/curve")
def get_curve(symbol: str = Query(...)):
    provider = get_provider()
    return ok(provider.snapshot_curve(symbol))
