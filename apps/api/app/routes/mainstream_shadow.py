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
