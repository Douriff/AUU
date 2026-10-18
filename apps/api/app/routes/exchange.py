"""Exchange connect API.

User:  /api/v1/exchange/*        (login required; feature toggle + allowlist)
Admin: /api/v1/admin/exchange/*  (admin only; toggle default OFF, allowlist, kill switch, caps)
Separate from paper and from /api/v1/live (liveEnabled is untouched).
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from app.auth.accounts import auth_enabled, find_user, user_by_id
from app.exchange import service as svc
from app.exchange import futures as fut
from app.exchange import store
from app.exchange.service import ServiceError
from app.routes.auth import session_user
from app.routes.envelope import err, ok

router = APIRouter(tags=["exchange"])


def _user(request: Request):
    if not auth_enabled():
        return {"id": "local", "is_admin": True, "name": "local"}
    return session_user(request)


def _fail(e: ServiceError):
    return err(e.code, e.message, e.status)


def _need(request: Request, *, gate: bool = True, kind: str = "read"):
    u = _user(request)
    if u is None:
        raise ServiceError("AUTH_REQUIRED", "请先登录", 401)
    if gate:
        svc.require(u)
    svc.rate(str(u["id"]), kind)
    return u


def _admin(request: Request):
    u = _user(request)
    if u is None:
        raise ServiceError("AUTH_REQUIRED", "请先登录", 401)
    if not u.get("is_admin"):
        raise ServiceError("ADMIN_REQUIRED", "只有管理员可以操作", 403)
    return u


class BindBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    apiKey: str = Field(max_length=200)
    secret: str = Field(max_length=200)
    passphrase: str = Field("", max_length=64)


class OrderBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    clientOrderId: str = Field(max_length=40)
    symbol: str = Field(max_length=24)
    side: str = Field(max_length=8)
    type: str = Field(max_length=8)
    amount: float
    price: Optional[float] = None
    confirmed: bool = False  # set by the web UI only after its confirmation dialog; the server refuses without it


class CancelBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    orderId: str = Field(max_length=64)
    symbol: str = Field(max_length=24)


@router.get("/api/v1/exchange/config")
def config(request: Request):
    u = _user(request)
    if u is None:
        return err("AUTH_REQUIRED", "请先登录", 401)
    return ok(svc.public_config(u))


@router.get("/api/v1/exchange/availability")
def availability(request: Request):
    """Per exchange x market: can the signed-in user open / close now, else the reason (no exchange call).
    The order endpoints enforce the very same rules, so the UI state is never the only guard."""
    u = _user(request)
    if u is None:
        return err("AUTH_REQUIRED", "请先登录", 401)
    return ok(svc.availability(u))


@router.get("/api/v1/exchange/{exchange}/quote")
def quote(exchange: str, request: Request, symbol: str = Query(..., max_length=32), market: str = Query("spot", max_length=4)):
    try:
        u = _need(request)
        return ok(svc.quote(str(u["id"]), exchange, symbol, market))
    except ServiceError as e:
        return _fail(e)


@router.get("/api/v1/exchange/{exchange}/supported")
def supported(exchange: str, request: Request, symbols: str = Query(..., max_length=2000), market: str = Query("spot", max_length=4)):
    try:
        u = _need(request)
        names = [x.strip() for x in symbols.split(",") if x.strip()][:200]
        return ok({"market": market, "supported": svc.supported(str(u["id"]), exchange, names, market)})
    except ServiceError as e:
        return _fail(e)


@router.get("/api/v1/exchange/accounts")
def accounts(request: Request):
    try:
        u = _need(request)
    except ServiceError as e:
        return _fail(e)
    return ok({"items": store.list_creds(str(u["id"]))})


@router.post("/api/v1/exchange/{exchange}/bind")
def bind(exchange: str, request: Request, body: BindBody):
    try:
        u = _need(request, kind="write")
        return ok(svc.bind(str(u["id"]), exchange, body.apiKey, body.secret, body.passphrase))
    except ServiceError as e:
        return _fail(e)


@router.delete("/api/v1/exchange/{exchange}/bind")
def unbind(exchange: str, request: Request):
    # unbind is allowed even when the feature is off: users can always delete their key
    try:
        u = _need(request, gate=False, kind="write")
        return ok({"deleted": svc.unbind(str(u["id"]), exchange)})
    except ServiceError as e:
        return _fail(e)


@router.get("/api/v1/exchange/{exchange}/balances")
def balances(exchange: str, request: Request):
    try:
        u = _need(request)
        return ok({"items": svc.balances(str(u["id"]), exchange)})
    except ServiceError as e:
        return _fail(e)


@router.get("/api/v1/exchange/{exchange}/orders/open")
def open_orders(exchange: str, request: Request, symbol: Optional[str] = Query(None, max_length=24)):
    try:
        u = _need(request)
        return ok({"items": svc.open_orders(str(u["id"]), exchange, symbol)})
    except ServiceError as e:
        return _fail(e)


@router.get("/api/v1/exchange/{exchange}/orders/history")
def history(exchange: str, request: Request, symbol: Optional[str] = Query(None, max_length=24), limit: int = Query(50, ge=1, le=100)):
    try:
        u = _need(request)
        return ok({"items": svc.history(str(u["id"]), exchange, symbol, limit)})
    except ServiceError as e:
        return _fail(e)


@router.post("/api/v1/exchange/{exchange}/orders")
def place(exchange: str, request: Request, body: OrderBody):
    try:
        u = _need(request, kind="write")
        return ok(svc.place(str(u["id"]), exchange, body.model_dump()))
    except ServiceError as e:
        return _fail(e)


@router.post("/api/v1/exchange/{exchange}/orders/cancel")
def cancel(exchange: str, request: Request, body: CancelBody):
    try:
        u = _need(request, kind="write")
        return ok(svc.cancel(str(u["id"]), exchange, body.orderId, body.symbol))
    except ServiceError as e:
        return _fail(e)


# ---- futures (USDT perpetual) ----
class FutOrderBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    clientOrderId: str = Field(max_length=40)
    symbol: str = Field(max_length=32)
    side: str = Field(max_length=8)
    type: str = Field(max_length=8)
    amount: float
    price: Optional[float] = None
    reduceOnly: bool = False
    takeProfit: Optional[float] = None
    stopLoss: Optional[float] = None
    confirmed: bool = False
    leverage: Optional[int] = None  # live view: applied before an opening order, so the dialog shows what is used
    marginMode: Optional[str] = Field(None, max_length=10)


class LeverageBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    symbol: str = Field(max_length=32)
    leverage: int
    marginMode: str = Field(max_length=10)


class SymBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    symbol: str = Field(max_length=32)


class ClosePosBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    symbol: str = Field(max_length=32)
    clientOrderId: str = Field(max_length=40)
    confirmed: bool = False


@router.post("/api/v1/exchange/futures/ack")
def futures_ack(request: Request):
    try:
        u = _need(request, kind="write")
        fut.ack(str(u["id"]))
        return ok({"futuresAck": True})
    except ServiceError as e:
        return _fail(e)


@router.get("/api/v1/exchange/{exchange}/futures/positions")
def futures_positions(exchange: str, request: Request):
    try:
        u = _need(request)
        return ok({"items": fut.positions(str(u["id"]), exchange)})
    except ServiceError as e:
        return _fail(e)


@router.get("/api/v1/exchange/{exchange}/futures/orders/open")
def futures_open(exchange: str, request: Request, symbol: Optional[str] = Query(None, max_length=32)):
    try:
        u = _need(request)
        return ok({"items": fut.open_orders(str(u["id"]), exchange, symbol)})
    except ServiceError as e:
        return _fail(e)


@router.get("/api/v1/exchange/{exchange}/futures/funding")
def futures_funding(exchange: str, request: Request, symbol: str = Query(..., max_length=32)):
    try:
        u = _need(request)
        return ok(fut.funding(str(u["id"]), exchange, symbol))
    except ServiceError as e:
        return _fail(e)


@router.post("/api/v1/exchange/{exchange}/futures/leverage")
def futures_leverage(exchange: str, request: Request, body: LeverageBody):
    try:
        u = _need(request, kind="write")
        return ok(fut.set_leverage(str(u["id"]), exchange, body.symbol, body.leverage, body.marginMode))
    except ServiceError as e:
        return _fail(e)


@router.post("/api/v1/exchange/{exchange}/futures/orders")
def futures_place(exchange: str, request: Request, body: FutOrderBody):
    try:
        u = _need(request, kind="write")
        return ok(fut.place(str(u["id"]), exchange, body.model_dump()))
    except ServiceError as e:
        return _fail(e)


@router.post("/api/v1/exchange/{exchange}/futures/orders/cancel")
def futures_cancel(exchange: str, request: Request, body: CancelBody):
    try:
        u = _need(request, kind="write")
        return ok(fut.cancel(str(u["id"]), exchange, body.orderId, body.symbol))
    except ServiceError as e:
        return _fail(e)


@router.post("/api/v1/exchange/{exchange}/futures/close")
def futures_close(exchange: str, request: Request, body: ClosePosBody):
    try:
        u = _need(request, kind="write")
        return ok(fut.close_position(str(u["id"]), exchange, body.symbol, body.clientOrderId, body.confirmed))
    except ServiceError as e:
        return _fail(e)


# ---- admin ----
class AdminSettingsBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: Optional[bool] = None
    allowlistOnly: Optional[bool] = None
    killSwitch: Optional[bool] = None
    maxOrderUsdt: Optional[float] = Field(None, gt=0, le=100000)
    maxFuturesUsdt: Optional[float] = Field(None, gt=0, le=100000)
    maxLeverage: Optional[int] = Field(None, ge=1, le=20)


class AllowBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user: str = Field(max_length=64)
    allow: bool = True
    capUsdt: Optional[float] = Field(None, gt=0, le=100000)
    futuresCapUsdt: Optional[float] = Field(None, gt=0, le=100000)


class CloseAllBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user: str = Field(max_length=64)


def _admin_view():
    s = store.settings()
    allow = []
    for uid in s["allowlist"]:
        row = user_by_id(uid) or {}
        allow.append({"id": uid, "name": row.get("name") or "?", "capUsdt": s["userCaps"].get(uid), "futuresCapUsdt": s["userFuturesCaps"].get(uid), "futuresAck": uid in s["futuresAcks"]})
    return {**s, "allowlistUsers": allow, "boundCounts": store.count_creds(), "serverIps": svc.SERVER_IPS, "cryptoReady": svc.crypto.available()}


@router.get("/api/v1/admin/exchange")
def admin_get(request: Request):
    try:
        _admin(request)
    except ServiceError as e:
        return _fail(e)
    return ok(_admin_view())


@router.put("/api/v1/admin/exchange")
def admin_put(request: Request, body: AdminSettingsBody):
    try:
        a = _admin(request)
    except ServiceError as e:
        return _fail(e)
    changes = {k: v for k, v in body.model_dump().items() if v is not None}
    for k, v in changes.items():
        store.set_setting(k, v)
    store.audit("*", str(a["id"]), "admin_settings", changes)
    return ok(_admin_view())


@router.post("/api/v1/admin/exchange/allowlist")
def admin_allow(request: Request, body: AllowBody):
    try:
        a = _admin(request)
    except ServiceError as e:
        return _fail(e)
    target = find_user(body.user) or user_by_id(body.user)
    if not target:
        return err("USER_NOT_FOUND", "找不到这个用户", 404)
    uid = str(target["id"])
    s = store.settings()
    allow = [x for x in s["allowlist"] if x != uid]
    caps = dict(s["userCaps"])
    fcaps = dict(s["userFuturesCaps"])
    if body.allow:
        allow.append(uid)
        if body.capUsdt is not None:
            caps[uid] = body.capUsdt
        if body.futuresCapUsdt is not None:
            fcaps[uid] = body.futuresCapUsdt
    else:
        caps.pop(uid, None)
        fcaps.pop(uid, None)
    store.set_setting("allowlist", allow)
    store.set_setting("userCaps", caps)
    store.set_setting("userFuturesCaps", fcaps)
    store.audit(uid, str(a["id"]), "admin_allowlist", {"allow": body.allow, "capUsdt": body.capUsdt, "futuresCapUsdt": body.futuresCapUsdt})
    return ok(_admin_view())


@router.post("/api/v1/admin/exchange/close-all")
def admin_close_all(request: Request, body: CloseAllBody):
    try:
        a = _admin(request)
    except ServiceError as e:
        return _fail(e)
    target = find_user(body.user) or user_by_id(body.user)
    if not target:
        return err("USER_NOT_FOUND", "找不到这个用户", 404)
    return ok(fut.close_all(str(target["id"]), str(a["id"])))


@router.get("/api/v1/admin/exchange/audit")
def admin_audit(request: Request, user: Optional[str] = Query(None, max_length=64), limit: int = Query(100, ge=1, le=500)):
    try:
        _admin(request)
    except ServiceError as e:
        return _fail(e)
    uid = None
    if user:
        t = find_user(user) or user_by_id(user)
        uid = str(t["id"]) if t else user
    return ok({"items": store.audit_rows(uid, limit)})
