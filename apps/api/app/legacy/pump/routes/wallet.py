"""Non-custodial wallet HTTP. Stores pubkeys and signatures only."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Query, Request

from app.auth.accounts import auth_enabled
from app.routes.auth import session_user
from app.routes.envelope import err, ok
from app.legacy.pump.wallet import orders, service

router = APIRouter(prefix="/api/v1/wallet", tags=["wallet"])

_MESSAGES = {
    "BAD_PUBKEY": "公钥格式不正确",
    "BAD_SIGNATURE": "签名无法通过校验",
    "CHALLENGE": "绑定挑战已过期或不匹配，请重新签名",
    "PUBKEY_TAKEN": "这个公钥已经绑定到其他账户",
    "BAD_RISK": "风控参数不正确",
    "RISK_NOTIONAL": "单笔上限必须大于 0，且不能超过 1 SOL",
    "RISK_DAY_LOSS": "日亏熔断必须大于 0，且不能超过 4.5%",
    "RISK_POSITIONS": "同时持仓须在 1 到 10 之间",
    "WALLET_HALT": "管理员已关闭所有人的钱包模式",
    "WALLET_OFF": "钱包模式总开关关闭",
    "WALLET_DEVNET_ONLY": "当前只允许 devnet",
    "WALLET_MAINNET_ONLY": "真钱单只在主网总开关打开时可用",
    "SLIPPAGE": "滑点不能超过 150 bps",
    "TAMPER": "交易内容和服务器组出的不一致",
    "NO_POSITION": "没有可卖出的钱包仓位",
    "BAD_PRICE": "缺少有效参考价",
    "BAD_MINT": "mint 格式不正确",
    "BAD_TX": "交易格式不正确",
    "WALLET_ROUTE": "组单失败，请稍后再试",
    "DAY_LOSS": "日亏已到上限，买入已停止",
    "NOT_BOUND": "请先绑定钱包公钥",
    "RISK_CONSENT": "开启钱包模式前需要勾选风险提示",
    "WALLET_MODE_OFF": "请先开启钱包模式并确认风险提示",
    "WALLET_RPC": "RPC 暂时不可用",
    "PREPARE": "待签名交易已过期，请重新生成",
    "AUTH_OFF": "绑定钱包需要开启账户（AUU_AUTH=on）并登录",
    "BAD_BODY": "请求格式不正确",
}


def _fail(exc: ValueError):
    code = str(exc)
    return err(code, _MESSAGES.get(code, "请求无效"), 400)


async def _json(request: Request) -> dict | object:
    try:
        raw = await request.json()
    except Exception:
        return err("BAD_BODY", _MESSAGES["BAD_BODY"], 400)
    if not isinstance(raw, dict):
        return err("BAD_BODY", _MESSAGES["BAD_BODY"], 400)
    return raw


def _actor(request: Request):
    if not auth_enabled():
        return None, err("AUTH_OFF", _MESSAGES["AUTH_OFF"], 400)
    user = session_user(request)
    if user is None:
        return None, err("AUTH_REQUIRED", "请先登录", 401)
    return user, None


def _text(raw: dict, key: str) -> str:
    value = raw.get(key)
    return value if isinstance(value, str) else ""


@router.get("/status")
def wallet_status(request: Request):
    user = session_user(request) if auth_enabled() else None
    user_id = str(user["id"]) if user else None
    return ok(service.status_for(user_id))


@router.post("/challenge")
async def wallet_challenge(request: Request):
    user, error = _actor(request)
    if error is not None:
        return error
    raw = await _json(request)
    if not isinstance(raw, dict):
        return raw
    try:
        body = service.issue_challenge(str(user["id"]), str(user.get("name") or ""), _text(raw, "pubkey"))
    except ValueError as exc:
        return _fail(exc)
    return ok(body)


@router.post("/bind")
async def wallet_bind(request: Request):
    user, error = _actor(request)
    if error is not None:
        return error
    raw = await _json(request)
    if not isinstance(raw, dict):
        return raw
    try:
        body = service.bind_pubkey(str(user["id"]), _text(raw, "pubkey"), _text(raw, "nonce"), _text(raw, "signature"))
    except ValueError as exc:
        return _fail(exc)
    return ok(body)


@router.post("/unbind")
def wallet_unbind(request: Request):
    user, error = _actor(request)
    if error is not None:
        return error
    return ok(service.unbind(str(user["id"])))


@router.put("/risk")
async def wallet_risk(request: Request):
    user, error = _actor(request)
    if error is not None:
        return error
    raw = await _json(request)
    if not isinstance(raw, dict):
        return raw
    try:
        sol = raw.get("max_notional_sol")
        day = raw.get("max_day_loss_pct")
        slots = raw.get("max_open_positions")
        if isinstance(sol, bool) or isinstance(day, bool) or isinstance(slots, bool):
            raise ValueError("BAD_RISK")
        if not isinstance(sol, (int, float)) or not isinstance(day, (int, float)):
            raise ValueError("BAD_RISK")
        if isinstance(slots, float) or not isinstance(slots, int):
            raise ValueError("BAD_RISK")
        body = service.update_risk(str(user["id"]), float(sol), float(day), slots)
    except ValueError as exc:
        return _fail(exc)
    return ok(body)


@router.post("/mode")
async def wallet_mode(request: Request):
    user, error = _actor(request)
    if error is not None:
        return error
    raw = await _json(request)
    if not isinstance(raw, dict):
        return raw
    enabled = raw.get("enabled")
    if not isinstance(enabled, bool):
        return err("BAD_BODY", _MESSAGES["BAD_BODY"], 400)
    try:
        body = service.set_wallet_mode(str(user["id"]), enabled, raw.get("accept_risk") is True)
    except ValueError as exc:
        return _fail(exc)
    return ok(body)


@router.post("/devnet/prepare")
def wallet_prepare(request: Request):
    user, error = _actor(request)
    if error is not None:
        return error
    try:
        body = service.prepare_memo(str(user["id"]))
    except ValueError as exc:
        return _fail(exc)
    return ok(body)


@router.post("/devnet/record")
async def wallet_record(request: Request):
    user, error = _actor(request)
    if error is not None:
        return error
    raw = await _json(request)
    if not isinstance(raw, dict):
        return raw
    try:
        body = service.record_signed(str(user["id"]), _text(raw, "prepare_id"), _text(raw, "signature"))
    except ValueError as exc:
        return _fail(exc)
    return ok(body)


@router.get("/signal")
def wallet_signal(
    request: Request,
    mint: str = Query(""),
    price_sol: Optional[float] = Query(None),
):
    from app.auth.accounts import auth_enabled

    user = session_user(request) if auth_enabled() else None
    user_id = str(user["id"]) if user else None
    return ok(orders.recommendation(user_id, mint.strip(), price_sol))


@router.post("/order/prepare")
async def wallet_order_prepare(request: Request):
    user, error = _actor(request)
    if error is not None:
        return error
    raw = await _json(request)
    if not isinstance(raw, dict):
        return raw
    side = _text(raw, "side")
    try:
        body = orders.prepare_order(
            str(user["id"]),
            mint=_text(raw, "mint"),
            side=side,
            notional_sol=raw.get("notional_sol"),
            sell_pct=raw.get("sell_pct"),
            price_sol=raw.get("price_sol"),
            slippage_bps=raw.get("slippage_bps"),
        )
    except ValueError as exc:
        return _fail(exc)
    return ok(body)


@router.post("/order/submit")
async def wallet_order_submit(request: Request):
    user, error = _actor(request)
    if error is not None:
        return error
    raw = await _json(request)
    if not isinstance(raw, dict):
        return raw
    try:
        body = orders.submit_order(str(user["id"]), _text(raw, "prepare_id"), _text(raw, "signed_tx"))
    except ValueError as exc:
        return _fail(exc)
    return ok(body)


@router.get("/ledger")
def wallet_ledger(request: Request, limit: int = Query(50, ge=1, le=100)):
    user, error = _actor(request)
    if error is not None:
        return error
    return ok(service.ledger_for(str(user["id"]), limit))


@router.post("/admin/halt")
async def wallet_halt(request: Request):
    user, error = _actor(request)
    if error is not None:
        return error
    if not user.get("is_admin"):
        return err("FORBIDDEN", "只有管理员可以关闭钱包模式", 403)
    raw = await _json(request)
    if not isinstance(raw, dict):
        return raw
    if not isinstance(raw.get("halt"), bool):
        return err("BAD_BODY", _MESSAGES["BAD_BODY"], 400)
    return ok(service.set_global_halt(bool(raw["halt"])))
