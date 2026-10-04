"""Shadow hypothesis record (no capital). Login required when AUU_AUTH is on."""
from __future__ import annotations

from fastapi import APIRouter, Request

from app.routes.envelope import err, ok
from app.routes.mainstream_paper import _uid

router = APIRouter(prefix="/api/v1/mainstream/shadow", tags=["mainstream-shadow"])


@router.get("/s3")
def shadow_s3(request: Request):
    # The auth gate already answers 401; checked again here so the route never leaks without it.
    if _uid(request) is None:
        return err("AUTH_REQUIRED", "请先登录", 401)
    from app.paper.shadow_s3 import get_shadow

    return ok(get_shadow().summary())


@router.get("/h2")
def shadow_h2(request: Request):
    """H2 forward shadow (trend + carry + idle yield): read-only summary of shadow_h2.sqlite."""
    if _uid(request) is None:
        return err("AUTH_REQUIRED", "请先登录", 401)
    from app.paper.shadow_h2 import peek_shadow

    s = peek_shadow()
    if s is None:
        return ok({"label": "H2 影子盘尚未开始记录", "ledger": None})
    return ok(s.summary())
