"""GET /api/v1/universe — pump.fun market table. Read-only, paper flags only."""
from __future__ import annotations

from fastapi import APIRouter, Query

from app.marketdata.universe import list_universe
from app.routes.envelope import ok

router = APIRouter(prefix="/api/v1", tags=["universe"])


@router.get("/universe")
def universe(
    tab: str = Query("hot"),
    offset: int = Query(0, ge=0),
    limit: int = Query(60, ge=1, le=100),
    q: str = Query(""),
    sort: str = Query(""),
    dir: str = Query(""),
    mints: str = Query(""),
):
    return ok(
        list_universe(
            tab=tab,
            offset=offset,
            limit=limit,
            q=q,
            sort=sort,
            direction=dir,
            mints=mints,
        )
    )
