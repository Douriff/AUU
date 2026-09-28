"""GET /api/v1/markets — read-only monitored-token list. Never enables live."""
from __future__ import annotations

from fastapi import APIRouter

from app.paper.markets import build_markets
from app.routes.envelope import ok

router = APIRouter(prefix="/api/v1", tags=["markets"])


@router.get("/markets")
def markets():
    return ok(build_markets())
