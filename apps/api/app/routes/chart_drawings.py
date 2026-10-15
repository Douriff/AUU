"""Per-user chart drawings: GET / PUT /api/v1/chart/drawings (login required).

GET /api/v1/chart/drawings?symbol=BTC[&tf=1h]      -> {symbol, tf, drawings[], updatedAt}
PUT /api/v1/chart/drawings  {symbol, tf?, drawings[], baseUpdatedAt?}
GET /api/v1/chart/drawings/index                    -> which charts have drawings

Chart UI data only: never touches ledgers, strategy, points or exchange code.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Request

from app import chart_drawings as cd
from app.routes.envelope import err, ok
from app.routes.mainstream_paper import _uid

router = APIRouter(prefix="/api/v1/chart/drawings", tags=["chart-drawings"])


def _gate(request: Request, limiter: cd.RateLimiter):
    uid = _uid(request)
    if uid is None:
        return None, err("AUTH_REQUIRED", "请先登录", 401)
    if not limiter.allow(uid):
        return None, err("RATE_LIMITED", "too many requests", 429, {"retryAfterSec": int(limiter.window)})
    return uid, None


@router.get("")
def get_drawings(request: Request, symbol: str = Query(..., min_length=1, max_length=32), tf: str = Query("", max_length=8)):
    uid, denied = _gate(request, cd.READ_LIMIT)
    if denied is not None:
        return denied
    try:
        return ok(cd.get_store().get(uid, cd.normalize_symbol(symbol), cd.normalize_tf(tf)))
    except cd.DrawingsError as e:
        return err(e.code, str(e), 400)


@router.get("/index")
def drawings_index(request: Request):
    uid, denied = _gate(request, cd.READ_LIMIT)
    if denied is not None:
        return denied
    return ok({"charts": cd.get_store().list_charts(uid)})


@router.put("")
async def put_drawings(request: Request):
    uid, denied = _gate(request, cd.WRITE_LIMIT)
    if denied is not None:
        return denied
    raw = await request.body()
    if len(raw) > 64_000:
        return err("TOO_LARGE", "request body too large", 413)
    try:
        import json

        body: Any = json.loads(raw or b"{}")
    except ValueError:
        return err("BAD_JSON", "invalid JSON", 400)
    if not isinstance(body, dict):
        return err("BAD_JSON", "object expected", 400)
    try:
        symbol = cd.normalize_symbol(body.get("symbol"))
        tf = cd.normalize_tf(body.get("tf", ""))
        drawings = cd.validate_drawings(body.get("drawings"))
        base = body.get("baseUpdatedAt")
        if base is not None and (isinstance(base, bool) or not isinstance(base, int) or base < 0):
            return err("BAD_BASE", "baseUpdatedAt must be a non-negative integer", 400)
        return ok(cd.get_store().put(uid, symbol, tf, drawings, base_updated_at=base))
    except cd.Conflict as c:
        return err("CONFLICT", "drawings changed elsewhere", 409, {"current": c.current})
    except cd.DrawingsError as e:
        return err(e.code, str(e), 400)
