"""Mainstream paper trading for logged-in users: /api/v1/mainstream/paper/*.

Paper only. Every route needs a session when AUU_AUTH is on (the gate returns 401 first;
the route re-checks). With AUU_AUTH off (local single-user mode) one "local" account is used.
There is no live path here: any ``mode`` other than "paper" is refused with LIVE_API_LOCKED.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, ConfigDict

from app.auth.accounts import auth_enabled
from app.paper.mainstream_account import OrderError, get_accounts
from app.routes.auth import session_user
from app.routes.envelope import err, ok

router = APIRouter(prefix="/api/v1/mainstream/paper", tags=["mainstream-paper"])


class OrderBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    client_order_id: str
    symbol: str
    side: str
    type: str = "market"
    qty: Optional[float] = None
    notional: Optional[float] = None
    limit_price: Optional[float] = None
    mode: str = "paper"


def _uid(request: Request) -> Optional[str]:
    if not auth_enabled():
        return "local"
    user = session_user(request)
    return str(user["id"]) if user else None


def _need_login():
    return err("AUTH_REQUIRED", "请先登录", 401)


@router.get("/account")
def account(request: Request, symbol: Optional[str] = Query(None), limit: int = Query(50, ge=1, le=200)):
    uid = _uid(request)
    if uid is None:
        return _need_login()
    sym = "".join(ch for ch in (symbol or "").split("/")[0].upper() if ch.isalnum()) or None
    return ok(get_accounts().snapshot(uid, sym, limit=limit))


@router.post("/orders")
def place(request: Request, body: OrderBody):
    uid = _uid(request)
    if uid is None:
        return _need_login()
    if body.mode != "paper":
        return err("LIVE_API_LOCKED", "实盘未开启：网页端不能下实盘单", 403)
    try:
        return ok(get_accounts().place(uid, body.model_dump()))
    except OrderError as e:
        return err(e.code, e.message, e.status)


@router.post("/orders/{order_id}/cancel")
def cancel(request: Request, order_id: str):
    uid = _uid(request)
    if uid is None:
        return _need_login()
    try:
        return ok(get_accounts().cancel(uid, order_id))
    except OrderError as e:
        return err(e.code, e.message, e.status)
