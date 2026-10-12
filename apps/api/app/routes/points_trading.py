"""Points trading API: /api/v1/points/trading/*. Capital = available points; paper routes untouched."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, ConfigDict

from app.auth.accounts import auth_enabled
from app.paper.mainstream_account import OrderError
from app.points import trading as pts
from app.routes.auth import session_user
from app.routes.envelope import err, ok

router = APIRouter(prefix="/api/v1/points/trading", tags=["points-trading"])


class OrderBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    client_order_id: str
    symbol: str
    side: str
    type: str = "market"
    qty: Optional[float] = None
    notional: Optional[float] = None
    limit_price: Optional[float] = None
    trigger_price: Optional[float] = None
    take_profit_price: Optional[float] = None
    stop_loss_price: Optional[float] = None
    mode: str = "points"


def _uid(request: Request) -> Optional[str]:
    if not auth_enabled():
        return "local"
    user = session_user(request)
    return str(user["id"]) if user else None


def _actor(request: Request):
    if not auth_enabled():
        return {"id": "local", "is_admin": True}
    return session_user(request)


def _need_login():
    return err("AUTH_REQUIRED", "请先登录", 401)


def _gate(request: Request):
    uid = _uid(request)
    if uid is None:
        return None, None, _need_login()
    actor = _actor(request)
    if not pts.enabled_for(bool(actor and actor.get("is_admin"))):
        return None, None, err("POINTS_TRADING_OFF", "积分交易暂未开放", 403)
    return uid, actor, None


@router.get("/config")
def config(request: Request):
    actor = _actor(request)
    if actor is None and auth_enabled():
        return _need_login()
    is_admin = bool(actor and actor.get("is_admin")) if actor else False
    return ok(pts.public_config(is_admin))


@router.get("/account")
def account(request: Request, symbol: Optional[str] = Query(None), limit: int = Query(50, ge=1, le=200)):
    uid, _actor_u, bad = _gate(request)
    if bad is not None:
        return bad
    sym = "".join(ch for ch in (symbol or "").split("/")[0].upper() if ch.isalnum()) or None
    return ok(pts.get_points_accounts().snapshot(uid, sym, limit=limit))


@router.post("/orders")
def place(request: Request, body: OrderBody):
    uid, _actor_u, bad = _gate(request)
    if bad is not None:
        return bad
    if body.mode not in ("points", "paper"):
        return err("LIVE_API_LOCKED", "实盘未开启：网页端不能下实盘单", 403)
    try:
        return ok(pts.get_points_accounts().place(uid, {**body.model_dump(), "mode": "points"}))
    except OrderError as e:
        return err(e.code, e.message, e.status, extra=e.extra())


@router.post("/orders/{order_id}/cancel")
def cancel(request: Request, order_id: str):
    uid, _actor_u, bad = _gate(request)
    if bad is not None:
        return bad
    try:
        return ok(pts.get_points_accounts().cancel(uid, order_id))
    except OrderError as e:
        return err(e.code, e.message, e.status, extra=e.extra())
