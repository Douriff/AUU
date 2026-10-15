"""Per-user price alerts: /api/v1/chart/alerts (login required; primary only).

GET    /api/v1/chart/alerts[?status=active|triggered|cancelled][&symbol=BTC]  -> {alerts[], caps, unread, now}
POST   /api/v1/chart/alerts   {symbol, price, direction: above|below|cross, note?, venue?}
DELETE /api/v1/chart/alerts/{id}
DELETE /api/v1/chart/alerts?scope=history                                       (clears triggered / cancelled rows)
GET    /api/v1/chart/alerts/notifications[?unread=1&limit=50]                  -> {events[], unread}
POST   /api/v1/chart/alerts/notifications/read   {ids?: [..]}                  (no ids = mark all read)

Chart UI data only: never touches ledgers, strategy, points or exchange code. On a standby node every
route answers 503 (the alert DB lives on the primary; the standby neither checks nor mails).
"""
from __future__ import annotations

import json
import time
from typing import Any

from fastapi import APIRouter, Query, Request

from app import price_alerts as pa
from app.chart_drawings import RateLimiter
from app.role import is_standby
from app.routes.envelope import err, ok
from app.routes.mainstream_paper import _uid

router = APIRouter(prefix="/api/v1/chart/alerts", tags=["price-alerts"])

WRITE_LIMIT = RateLimiter(limit=20, window=60.0)
READ_LIMIT = RateLimiter(limit=240, window=60.0)


def _gate(request: Request, limiter: RateLimiter):
    if is_standby():
        return None, err("STANDBY", "价格提醒由主节点处理，当前节点暂不可用", 503)
    uid = _uid(request)
    if uid is None:
        return None, err("AUTH_REQUIRED", "请先登录", 401)
    if not limiter.allow(uid):
        return None, err("RATE_LIMITED", "操作太频繁，请稍后再试", 429, {"retryAfterSec": int(limiter.window)})
    return uid, None


def _market():
    from app.marketdata.mainstream import get_service

    svc = get_service()
    return svc, svc.markets()


def _summary(uid: str, store: pa.PriceAlertStore) -> dict[str, Any]:
    c = store.counts(uid)
    return {"active": c.get("active", 0), "maxActive": pa.MAX_ACTIVE, "triggered": c.get("triggered", 0)}


@router.get("")
def list_alerts(request: Request, status: str = Query("", max_length=12), symbol: str = Query("", max_length=32)):
    uid, denied = _gate(request, READ_LIMIT)
    if denied is not None:
        return denied
    if status and status not in ("active", "triggered", "cancelled"):
        return err("BAD_STATUS", "状态参数不正确", 400)
    try:
        sym = pa.normalize_symbol(symbol) if symbol else None
    except pa.AlertError as e:
        return err(e.code, str(e), 400)
    store = pa.get_store()
    return ok({"alerts": store.list(uid, status=status or None, symbol=sym), "caps": _summary(uid, store), "unread": store.unread(uid),
               "now": int(time.time() * 1000), "emailOn": _has_email(uid)})


def _has_email(uid: str) -> bool:
    try:
        from app.auth import accounts

        u = accounts.user_by_id(uid)
        return bool(u and str(u.get("email") or "").strip())
    except Exception:
        return False


@router.post("")
async def create_alert(request: Request):
    uid, denied = _gate(request, WRITE_LIMIT)
    if denied is not None:
        return denied
    raw = await request.body()
    if len(raw) > 2_000:
        return err("TOO_LARGE", "请求内容过大", 413)
    try:
        body: Any = json.loads(raw or b"{}")
    except ValueError:
        return err("BAD_JSON", "请求格式不正确", 400)
    if not isinstance(body, dict):
        return err("BAD_JSON", "请求格式不正确", 400)
    try:
        symbol = pa.normalize_symbol(body.get("symbol"))
        price = pa.normalize_price(body.get("price"))
        direction = pa.normalize_direction(body.get("direction"))
        note = pa.normalize_note(body.get("note"))
        venue_req = pa.normalize_venue(body.get("venue"))
        svc, mk = _market()
        if symbol not in svc.cfg.all_symbols():
            return err("BAD_SYMBOL", "该币种暂不支持价格提醒", 400)
        now = int(time.time() * 1000)
        last = pa.fresh_prices(mk, now).get(symbol)
        side = None
        if last is not None:
            side = pa.side_of(last, price)
            if direction == "above" and side > 0:
                return err("ALREADY_REACHED", "当前价格已经达到或高于提醒价，请改用“跌到”或设置更高的价格", 400, {"last": last})
            if direction == "below" and side < 0:
                return err("ALREADY_REACHED", "当前价格已经低于提醒价，请改用“涨到”或设置更低的价格", 400, {"last": last})
        store = pa.get_store()
        row = store.create(uid, symbol, price, direction, venue=venue_req or str(mk.get("exchange") or ""), note=note, side=side, now_ms=now)
        return ok({"alert": row, "caps": _summary(uid, store), "last": last})
    except pa.AlertError as e:
        return err(e.code, str(e), e.status)


@router.get("/notifications")
def notifications(request: Request, unread: int = Query(0, ge=0, le=1), limit: int = Query(50, ge=1, le=100)):
    uid, denied = _gate(request, READ_LIMIT)
    if denied is not None:
        return denied
    store = pa.get_store()
    return ok({"events": store.events(uid, unread_only=bool(unread), limit=limit), "unread": store.unread(uid), "now": int(time.time() * 1000)})


@router.post("/notifications/read")
async def notifications_read(request: Request):
    uid, denied = _gate(request, WRITE_LIMIT)
    if denied is not None:
        return denied
    raw = await request.body()
    try:
        body: Any = json.loads(raw or b"{}")
    except ValueError:
        return err("BAD_JSON", "请求格式不正确", 400)
    ids = body.get("ids") if isinstance(body, dict) else None
    if ids is not None and (not isinstance(ids, list) or not all(isinstance(i, int) and not isinstance(i, bool) for i in ids)):
        return err("BAD_IDS", "参数不正确", 400)
    store = pa.get_store()
    n = store.mark_read(uid, ids)
    return ok({"marked": n, "unread": store.unread(uid)})


@router.delete("/{alert_id}")
def delete_alert(request: Request, alert_id: int):
    uid, denied = _gate(request, WRITE_LIMIT)
    if denied is not None:
        return denied
    store = pa.get_store()
    if not store.delete(uid, alert_id):
        return err("NOT_FOUND", "没有找到这条提醒", 404)
    return ok({"deleted": alert_id, "caps": _summary(uid, store)})


@router.delete("")
def clear_history(request: Request, scope: str = Query("", max_length=12)):
    uid, denied = _gate(request, WRITE_LIMIT)
    if denied is not None:
        return denied
    if scope != "history":
        return err("BAD_SCOPE", "参数不正确", 400)
    store = pa.get_store()
    return ok({"cleared": store.clear_history(uid), "caps": _summary(uid, store)})
