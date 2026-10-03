"""M3 strategy console API (read-only). Login required when AUU_AUTH is on."""
from __future__ import annotations

from fastapi import APIRouter, Request

from app.routes.envelope import err, ok
from app.routes.mainstream_paper import _uid

router = APIRouter(prefix="/api/v1/mainstream/strategy", tags=["mainstream-strategy"])


@router.get("")
def strategy_summary(request: Request):
    # The auth gate already answers 401; checked again here so the route never leaks without it.
    if _uid(request) is None:
        return err("AUTH_REQUIRED", "请先登录", 401)
    from app.paper.strategy_runner import get_runner

    return ok(get_runner().summary())
