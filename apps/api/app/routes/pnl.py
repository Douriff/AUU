"""盈亏 (P&L) of the caller's own manual paper account: /api/v1/pnl/*. Read-only, login required,
always the session user's id (no user parameter exists, so nobody can read another account)."""
from __future__ import annotations

from fastapi import APIRouter, Query, Request
from starlette.concurrency import run_in_threadpool

from app.paper.pnl import RANGES, get_pnl
from app.routes.envelope import ok
from app.routes.mainstream_paper import _need_login, _uid

router = APIRouter(prefix="/api/v1/pnl", tags=["pnl"])


@router.get("/summary")
async def pnl_summary(request: Request):
    uid = _uid(request)
    if uid is None:
        return _need_login()
    return ok(await run_in_threadpool(get_pnl().summary, uid))


@router.get("/curve")
async def pnl_curve(request: Request, range: str = Query("7d", max_length=8)):  # noqa: A002
    uid = _uid(request)
    if uid is None:
        return _need_login()
    rng = range if range in RANGES else "7d"
    return ok(await run_in_threadpool(get_pnl().curve, uid, rng))
