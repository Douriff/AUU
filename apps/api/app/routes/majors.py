"""GET /api/v1/majors — public CEX tickers. No keys, no orders."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Query

from app.marketdata.cex import build_majors, compare_symbol
from app.routes.envelope import ok

router = APIRouter(prefix="/api/v1", tags=["majors"])


@router.get("/majors")
def majors():
    return ok(build_majors())


@router.get("/majors/compare")
def majors_compare(base: str = Query(""), onchain_usd: Optional[float] = Query(None)):
    return ok(compare_symbol(base, onchain_usd))
