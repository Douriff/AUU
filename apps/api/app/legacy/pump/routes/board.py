"""GET /api/v1/board — read-only 盘面 aggregate. Never enables live."""
from __future__ import annotations

from fastapi import APIRouter

from app.legacy.pump.paper.board import build_board
from app.routes.envelope import ok

router = APIRouter(prefix="/api/v1", tags=["board"])


@router.get("/board")
def board():
    return ok(build_board())
