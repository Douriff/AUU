"""GET /api/v1/majors — public CEX tickers. No keys, no orders."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Query

from app.marketdata.cex import build_majors, compare_symbol
from app.marketdata.tickers import list_tickers
from app.routes.envelope import ok

router = APIRouter(prefix="/api/v1", tags=["majors"])


@router.get("/majors")
def majors():
    return ok(build_majors())


@router.get("/majors/tickers")
def majors_tickers(
    venue: str = Query("binance"),
    limit: int = Query(100, ge=1, le=100),
    q: str = Query(""),
    sort: str = Query("volume"),
    dir: str = Query("desc"),
    bucket: str = Query("all"),
):
    return ok(list_tickers(venue=venue, limit=limit, q=q, sort=sort, direction=dir, bucket=bucket))


@router.get("/majors/compare")
def majors_compare(base: str = Query(""), onchain_usd: Optional[float] = Query(None)):
    return ok(compare_symbol(base, onchain_usd))
