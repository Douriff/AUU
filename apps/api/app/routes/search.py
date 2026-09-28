"""GET /api/v1/search — pump.fun universe lookup. Read-only, paper flags only."""
from __future__ import annotations

from fastapi import APIRouter, Query

from app.marketdata.pump_search import coin_detail, search_coins
from app.routes.envelope import err, ok

router = APIRouter(prefix="/api/v1", tags=["search"])


@router.get("/search")
def search(q: str = Query(""), limit: int = Query(20, ge=1, le=25)):
    return ok(search_coins(q, limit=limit))


@router.get("/search/coin")
def search_coin(mint: str = Query("")):
    detail = coin_detail(mint)
    if detail is None:
        return err("UNKNOWN_SYMBOL", "找不到这个代币", 404)
    return ok(detail)
