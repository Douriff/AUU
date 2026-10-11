"""Points (积分) API. Accounts only (AUU_AUTH=on); separate from every paper / strategy route.

User:   GET  /points/me · GET /points/topup · POST /points/topup · POST /points/spend
Admin:  GET  /admin/points/users · GET /admin/points/user/{id} · GET /admin/points/audit
        GET  /admin/points/verify · GET /admin/points/export.csv
        POST /admin/points/adjust · POST /admin/points/manual-topup · POST /admin/points/refund
Every admin write needs password + 2FA (admin_step_up) and a reason; every write needs an
idempotency key (header Idempotency-Key or body idempotency_key). All attempts are audited.
There is no withdrawal and no transfer endpoint.
"""
from __future__ import annotations

import csv
import io
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import APIRouter, Query, Request
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool

from app.auth.accounts import admin_step_up, allow_attempt, auth_enabled, list_users, user_by_id
from app.points import providers, service, trc20
from app.points.ledger import PointsError, get_points_ledger
from app.routes.auth import _ip, session_user
from app.routes.envelope import err, ok

_LOG = logging.getLogger("auu.points")
router = APIRouter(prefix="/api/v1", tags=["points"])

_BJ = timezone(timedelta(hours=8))
_MESSAGES = {
    "AUTH_OFF": "积分需要开启账户登录",
    "AUTH_REQUIRED": "请先登录",
    "FORBIDDEN": "只有管理员可以操作积分",
    "UNKNOWN_USER": "没有这个账户",
    "RATE_LIMIT": "尝试过于频繁，请稍后再试",
    "BAD_BODY": "请求格式不正确",
    "BAD_LOGIN": "密码不正确",
    "TOTP_REQUIRED": "请输入两步验证码（或恢复码）",
    "TOTP_BAD": "两步验证码不正确或已使用",
    "POINTS_2FA_REQUIRED": "管理员需先在设置里开启两步验证，才能调整积分",
    "POINTS_BAD_AMOUNT": "积分数量不正确",
    "POINTS_REASON_REQUIRED": "请填写原因",
    "POINTS_REASON_TOO_LONG": "原因不能超过 200 字",
    "POINTS_IDEMPOTENCY_REQUIRED": "缺少幂等键，请刷新页面后重试",
    "POINTS_IDEMPOTENCY_CONFLICT": "同一请求编号已用于另一笔操作，请刷新页面后重试",
    "POINTS_INSUFFICIENT": "积分余额不足",
    "POINTS_TOO_LARGE": "积分余额超出上限",
    "POINTS_TOPUP_CLOSED": "充值暂未开放",
    "POINTS_MANUAL_OFF": "管理员手工入账通道未开启",
    "POINTS_SHOP_OFF": "积分兑换暂未开放",
    "POINTS_UNKNOWN_ITEM": "没有这个兑换项",
    "POINTS_NOT_REFUNDABLE": "这条流水不能退款",
    "POINTS_REFUND_EXCEEDS": "退款超过了这条流水剩余可退的积分",
    "POINTS_UNKNOWN_ORDER": "订单不存在",
    "POINTS_BAD_REQUEST": "请求无效",
    "TRC20_BAD_AMOUNT": "充值金额不在允许范围内",
    "TRC20_TOO_MANY_PENDING": "待支付订单太多，请先完成或等待过期",
    "TRC20_DAILY_LIMIT": "已超过今日充值上限",
    "TRC20_BUSY": "该金额暂时无法下单，请换一个金额",
    "TRC20_UNKNOWN_TX": "没有这笔链上交易",
    "TRC20_NOT_PENDING": "这笔交易已处理过",
}
_STATUS = {"TRC20_TOO_MANY_PENDING": 429, "TRC20_DAILY_LIMIT": 429, "TRC20_UNKNOWN_TX": 404, "TRC20_NOT_PENDING": 409,
           "AUTH_REQUIRED": 401, "BAD_LOGIN": 401, "TOTP_BAD": 401, "FORBIDDEN": 403, "POINTS_2FA_REQUIRED": 403,
           "UNKNOWN_USER": 404, "RATE_LIMIT": 429, "POINTS_IDEMPOTENCY_CONFLICT": 409, "POINTS_TOPUP_CLOSED": 403,
           "POINTS_MANUAL_OFF": 403, "POINTS_SHOP_OFF": 403}


def _e(code: str):
    return err(code, _MESSAGES.get(code, "请求无效"), _STATUS.get(code, 400))


def _audit(request: Request, actor: str, action: str, result: str, target: str = "", detail: Optional[dict] = None) -> None:
    try:
        get_points_ledger().audit(actor=actor, action=action, result=result, target=target, ip=_ip(request),
                                  ua=request.headers.get("user-agent", ""), detail=detail)
    except Exception:  # the ledger write itself is what matters; never hide it behind an audit failure
        _LOG.exception("points audit write failed")


def _who(request: Request, *, admin: bool = False):
    if not auth_enabled():
        return None, _e("AUTH_OFF")
    actor = session_user(request)
    if actor is None:
        return None, _e("AUTH_REQUIRED")
    if admin and not actor.get("is_admin"):
        _audit(request, f"user:{actor['id']}", "admin_access", "FORBIDDEN", detail={"path": request.url.path})
        return None, _e("FORBIDDEN")
    return actor, None


async def _body(request: Request):
    try:
        raw = await request.json()
    except Exception:
        return None
    return raw if isinstance(raw, dict) else None


def _idem(request: Request, raw: dict) -> Any:
    return request.headers.get("idempotency-key") or raw.get("idempotency_key")


def _text(raw: dict, key: str) -> str:
    v = raw.get(key)
    return v if isinstance(v, str) else ""


def _safe_detail(raw: dict) -> dict:
    """Request fields worth keeping in the audit log (never the password or 2FA code)."""
    return {k: raw.get(k) for k in ("user_id", "amount", "points", "reason", "entry_id", "item", "provider", "external_ref") if k in raw}


# ---- user -------------------------------------------------------------------------------------------
@router.get("/points/me")
def points_me(request: Request, limit: int = Query(50, ge=1, le=200), before: Optional[int] = Query(None)):
    actor, bad = _who(request)
    if bad is not None:
        return bad
    view = service.account_view(str(actor["id"]), limit=limit, before_id=before)
    return ok({**view, "topup": providers.topup_status(bool(actor.get("is_admin"))), "liveEnabled": False})


@router.get("/points/topup")
def points_topup_status(request: Request):
    actor, bad = _who(request)
    if bad is not None:
        return bad
    return ok({**providers.topup_status(bool(actor.get("is_admin"))), "min": service.TOPUP_MIN, "max": service.TOPUP_MAX})


@router.post("/points/topup")
async def points_topup(request: Request):
    actor, bad = _who(request)
    if bad is not None:
        return bad
    raw = await _body(request)
    if raw is None:
        return _e("BAD_BODY")
    uid = str(actor["id"])
    if not allow_attempt(f"points:{uid}"):
        _audit(request, f"user:{uid}", "topup", "RATE_LIMIT", uid)
        return _e("RATE_LIMIT")
    try:
        out = await run_in_threadpool(service.topup, uid, raw.get("provider"), raw.get("points"), _idem(request, raw))
    except PointsError as exc:
        _audit(request, f"user:{uid}", "topup", str(exc), uid, _safe_detail(raw))
        return _e(str(exc))
    _audit(request, f"user:{uid}", "topup", "replayed" if out.get("replayed") else str(out["order"]["status"]), uid,
           {**_safe_detail(raw), "order": out["order"]["order_id"]})
    return ok({**out, "balance": get_points_ledger().balance(uid)})


@router.post("/points/spend")
async def points_spend(request: Request):
    actor, bad = _who(request)
    if bad is not None:
        return bad
    raw = await _body(request)
    if raw is None:
        return _e("BAD_BODY")
    uid = str(actor["id"])
    if not allow_attempt(f"points:{uid}"):
        _audit(request, f"user:{uid}", "spend", "RATE_LIMIT", uid)
        return _e("RATE_LIMIT")
    try:
        row, replayed = await run_in_threadpool(service.spend, uid, raw.get("item"), _idem(request, raw))
    except PointsError as exc:
        _audit(request, f"user:{uid}", "spend", str(exc), uid, _safe_detail(raw))
        return _e(str(exc))
    _audit(request, f"user:{uid}", "spend", "replayed" if replayed else "ok", uid, {**_safe_detail(raw), "entry": row["id"]})
    return ok({"entry": service.public_entry(row), "replayed": replayed, "balance": get_points_ledger().balance(uid)})


# ---- admin ------------------------------------------------------------------------------------------
@router.get("/admin/points/users")
def admin_points_users(request: Request, q: str = Query("", max_length=64)):
    actor, bad = _who(request, admin=True)
    if bad is not None:
        return bad
    led = get_points_ledger()
    needle = q.strip().lower()
    items = []
    for u in list_users():
        if needle and needle not in str(u["name"]).lower() and needle not in str(u["display_name"]).lower() and needle != u["id"]:
            continue
        items.append({"id": u["id"], "name": u["name"], "display_name": u["display_name"], "balance": led.balance(u["id"])})
    return ok({"items": items[:200], "topup": providers.topup_status(True)})


@router.get("/admin/points/user/{user_id}")
def admin_points_user(request: Request, user_id: str, limit: int = Query(100, ge=1, le=500), before: Optional[int] = Query(None)):
    actor, bad = _who(request, admin=True)
    if bad is not None:
        return bad
    user = user_by_id(user_id)
    if user is None:
        return _e("UNKNOWN_USER")
    _audit(request, f"admin:{actor['id']}", "lookup", "ok", user_id)
    view = service.account_view(user_id, limit=limit, before_id=before, admin=True)
    return ok({**view, "user": {"id": user["id"], "name": user.get("name"), "display_name": user.get("display_name") or user.get("name")},
               "audit": get_points_ledger().audit_rows(30, target=user_id)})


@router.get("/admin/points/audit")
def admin_points_audit(request: Request, limit: int = Query(100, ge=1, le=500)):
    actor, bad = _who(request, admin=True)
    if bad is not None:
        return bad
    return ok({"items": get_points_ledger().audit_rows(limit)})


@router.get("/admin/points/verify")
def admin_points_verify(request: Request):
    actor, bad = _who(request, admin=True)
    if bad is not None:
        return bad
    out = get_points_ledger().verify()
    _audit(request, f"admin:{actor['id']}", "verify", "ok" if out["ok"] else "problems", detail={"entries": out["entries"]})
    return ok(out)


_CSV_COLS = ("id", "time_bj", "user_id", "user_name", "kind", "amount", "balance_after", "reason", "operator", "provider", "ref", "hash")


def _cell(value: Any) -> str:
    """Spreadsheet-safe text: a leading = + - @ tab or CR would be read as a formula."""
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


@router.get("/admin/points/export.csv")
def admin_points_export(request: Request, user_id: str = Query("", max_length=64)):
    actor, bad = _who(request, admin=True)
    if bad is not None:
        return bad
    uid = user_id.strip() or None
    if uid is not None and user_by_id(uid) is None:
        return _e("UNKNOWN_USER")
    names = {u["id"]: u["name"] for u in list_users()}
    rows = get_points_ledger().iter_all(uid)
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow(_CSV_COLS)
    for r in rows:
        when = datetime.fromtimestamp(int(r["ts"]) / 1000, tz=_BJ).strftime("%Y-%m-%d %H:%M:%S+08:00")
        w.writerow([r["id"], when, _cell(r["user_id"]), _cell(names.get(r["user_id"], "")), r["kind"], int(r["amount"]),
                    int(r["balance_after"]), _cell(r["reason"]), _cell(r["operator"]), _cell(r["provider"]), _cell(r["ref"]), r["hash"]])
    _audit(request, f"admin:{actor['id']}", "export_csv", "ok", uid or "*", {"rows": len(rows)})
    stamp = datetime.now(_BJ).strftime("%Y%m%d-%H%M")
    name = f"points-ledger-{uid or 'all'}-{stamp}.csv"
    return Response(content="\ufeff" + buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"})


async def _admin_write(request: Request, action: str, pre, fn):
    actor, bad = _who(request, admin=True)
    if bad is not None:
        return bad
    raw = await _body(request)
    if raw is None:
        return _e("BAD_BODY")
    aid = str(actor["id"])
    target = str(raw.get("user_id") or raw.get("entry_id") or "")
    if not allow_attempt(f"points-admin:{aid}"):
        _audit(request, f"admin:{aid}", action, "RATE_LIMIT", target, _safe_detail(raw))
        return _e("RATE_LIMIT")
    try:
        pre(raw)  # validate first so a typo does not burn the admin's one-time 2FA code
    except PointsError as exc:
        _audit(request, f"admin:{aid}", action, str(exc), target, _safe_detail(raw))
        return _e(str(exc))
    try:
        await run_in_threadpool(admin_step_up, aid, _text(raw, "password"), _text(raw, "totp_code"))
    except ValueError as exc:
        _audit(request, f"admin:{aid}", action, f"step_up:{exc}", target, _safe_detail(raw))
        return _e(str(exc))
    try:
        row, replayed = await run_in_threadpool(fn, aid, raw)
    except PointsError as exc:
        _audit(request, f"admin:{aid}", action, str(exc), target, _safe_detail(raw))
        return _e(str(exc))
    _audit(request, f"admin:{aid}", action, "replayed" if replayed else "ok", row["user_id"], {**_safe_detail(raw), "entry": row["id"]})
    _LOG.warning("POINTS %s by admin %s: user=%s amount=%s entry=%s%s", action, aid, row["user_id"], row["amount"], row["id"],
                 " (replayed)" if replayed else "")
    return ok({"entry": service.public_entry(row, admin=True), "replayed": replayed, "balance": get_points_ledger().balance(row["user_id"])})


def _known_user(raw: dict) -> str:
    uid = _text(raw, "user_id").strip()
    if not uid or user_by_id(uid) is None:
        raise PointsError("UNKNOWN_USER")
    return uid


@router.post("/admin/points/adjust")
async def admin_points_adjust(request: Request):
    def pre(raw):
        _known_user(raw)
        service.parse_signed(raw.get("amount"))
        service.parse_reason(raw.get("reason"))
        service.parse_idem(_idem(request, raw))

    return await _admin_write(request, "adjust", pre, lambda aid, raw: service.admin_adjust(
        aid, _known_user(raw), raw.get("amount"), raw.get("reason"), _idem(request, raw)))


@router.post("/admin/points/manual-topup")
async def admin_points_manual(request: Request):
    def pre(raw):
        if not providers.REGISTRY["manual_admin"].enabled():
            raise PointsError("POINTS_MANUAL_OFF")
        _known_user(raw)
        service.parse_amount(raw.get("points"), lo=service.TOPUP_MIN, hi=service.TOPUP_MAX)
        service.parse_reason(raw.get("reason"))
        service.parse_idem(_idem(request, raw))

    return await _admin_write(request, "manual_topup", pre, lambda aid, raw: service.admin_manual_topup(
        aid, _known_user(raw), raw.get("points"), raw.get("reason"), raw.get("external_ref"), _idem(request, raw)))


@router.post("/admin/points/refund")
async def admin_points_refund(request: Request):
    def pre(raw):
        eid = service.parse_amount(raw.get("entry_id"), lo=1, hi=10**10 - 1)
        target = get_points_ledger().entry(eid)
        if target is None or target["kind"] not in ("spend", "topup"):
            raise PointsError("POINTS_NOT_REFUNDABLE")
        service.parse_amount(raw.get("amount"), lo=1, hi=abs(int(target["amount"])))
        service.parse_reason(raw.get("reason"))
        service.parse_idem(_idem(request, raw))

    return await _admin_write(request, "refund", pre, lambda aid, raw: service.admin_refund(
        aid, raw.get("entry_id"), raw.get("amount"), raw.get("reason"), _idem(request, raw)))


# ---- USDT-TRC20 ------------------------------------------------------------------------------------
@router.get("/points/trc20")
def points_trc20(request: Request):
    actor, bad = _who(request)
    if bad is not None:
        return bad
    rows = trc20.user_orders(str(actor["id"]), 30)
    return ok({"config": trc20.public_config(bool(actor.get("is_admin"))), "orders": [trc20.order_view(o) for o in rows]})


@router.post("/points/trc20/orders")
async def points_trc20_order(request: Request):
    actor, bad = _who(request)
    if bad is not None:
        return bad
    raw = await _body(request)
    if raw is None:
        return _e("BAD_BODY")
    uid = str(actor["id"])
    if not allow_attempt(f"trc20-order:{uid}"):
        _audit(request, f"user:{uid}", "trc20_order", "RATE_LIMIT", uid)
        return _e("RATE_LIMIT")
    try:
        row, replayed = await run_in_threadpool(lambda: trc20.create_order(uid, raw.get("usdt"), _idem(request, raw),
                                                                             is_admin=bool(actor.get("is_admin"))))
    except PointsError as exc:
        _audit(request, f"user:{uid}", "trc20_order", str(exc), uid, {"usdt": raw.get("usdt")})
        return _e(str(exc))
    _audit(request, f"user:{uid}", "trc20_order", "replayed" if replayed else "ok", uid,
           {"order": row["order_id"], "amount": trc20.fmt_order_amount(int(row["amount_micro"]))})
    return ok({"order": trc20.order_view(row, qr=True), "replayed": replayed, "config": trc20.public_config(bool(actor.get("is_admin")))})


@router.get("/points/trc20/orders/{order_id}")
def points_trc20_get(request: Request, order_id: str):
    actor, bad = _who(request)
    if bad is not None:
        return bad
    o = trc20.get_order(order_id[:64])
    if o is None or (o["user_id"] != str(actor["id"]) and not actor.get("is_admin")):
        return _e("POINTS_UNKNOWN_ORDER")
    return ok({"order": trc20.order_view(o, qr=True)})


@router.get("/admin/points/trc20")
def admin_points_trc20(request: Request, limit: int = Query(200, ge=1, le=1000)):
    actor, bad = _who(request, admin=True)
    if bad is not None:
        return bad
    names = {u["id"]: u["name"] for u in list_users()}
    orders = [{**trc20.order_view(o, admin=True), "userName": names.get(o["user_id"], "")} for o in trc20.all_orders(limit)]
    pend = [{**trc20.tx_view(t), "userName": names.get(t.get("user_id") or "", "")} for t in trc20.txs("exception", limit)]
    recent = [{**trc20.tx_view(t), "userName": names.get(t.get("user_id") or "", "")} for t in trc20.txs(None, limit)]
    return ok({"config": {**trc20.public_config(True), "pollEnabled": trc20.poll_enabled()}, "orders": orders, "pending": pend,
               "transfers": recent, "reconcile": trc20.reconcile()})


def _trc20_pre(raw: dict, *, need_user: bool) -> None:
    tx = _text(raw, "tx_hash").strip().lower()
    if not tx or len(tx) != 64 or any(c not in "0123456789abcdef" for c in tx):
        raise PointsError("TRC20_UNKNOWN_TX")
    if need_user:
        _known_user(raw)
        service.parse_amount(raw.get("points"), lo=1, hi=service.TOPUP_MAX * 100)
    service.parse_reason(raw.get("reason"))


@router.post("/admin/points/trc20/resolve")
async def admin_points_trc20_resolve(request: Request):
    """Attribute a review-list transfer to a user: password + 2FA + reason, credited once per tx hash."""
    def pre(raw):
        _trc20_pre(raw, need_user=True)
        service.parse_idem(_idem(request, raw))

    def fn(aid, raw):
        entry = trc20.resolve(_text(raw, "tx_hash").strip().lower(), _known_user(raw),
                              service.parse_amount(raw.get("points"), lo=1, hi=service.TOPUP_MAX * 100),
                              service.parse_reason(raw.get("reason")), aid)
        return entry, False

    return await _admin_write(request, "trc20_resolve", pre, fn)


@router.post("/admin/points/trc20/dismiss")
async def admin_points_trc20_dismiss(request: Request):
    """Close a review-list transfer without crediting anyone (spam / refunded off-platform): password + 2FA + reason."""
    actor, bad = _who(request, admin=True)
    if bad is not None:
        return bad
    raw = await _body(request)
    if raw is None:
        return _e("BAD_BODY")
    aid = str(actor["id"])
    tx = _text(raw, "tx_hash").strip().lower()
    if not allow_attempt(f"points-admin:{aid}"):
        return _e("RATE_LIMIT")
    try:
        _trc20_pre(raw, need_user=False)
    except PointsError as exc:
        _audit(request, f"admin:{aid}", "trc20_dismiss", str(exc), tx)
        return _e(str(exc))
    try:
        await run_in_threadpool(admin_step_up, aid, _text(raw, "password"), _text(raw, "totp_code"))
    except ValueError as exc:
        _audit(request, f"admin:{aid}", "trc20_dismiss", f"step_up:{exc}", tx)
        return _e(str(exc))
    try:
        row = await run_in_threadpool(trc20.dismiss, tx, service.parse_reason(raw.get("reason")), aid)
    except PointsError as exc:
        _audit(request, f"admin:{aid}", "trc20_dismiss", str(exc), tx)
        return _e(str(exc))
    _audit(request, f"admin:{aid}", "trc20_dismiss", "ok", tx, {"reason": raw.get("reason")})
    return ok({"transfer": trc20.tx_view(row)})
