"""Business rules: access gate, bind/unbind, reads, orders (cap, kill switch, idempotency)."""
from __future__ import annotations

import re
import threading
import time
from collections import defaultdict, deque
from typing import Any, Optional

from app.exchange import crypto, store
from app.exchange.connector import EXCHANGES, ConnectorError, check_permissions, map_error, norm_balance, norm_order, user_client

SERVER_IPS = {"primary": "156.227.238.242", "standby": "156.239.51.157"}
ALLOWED_QUOTES = ("USDT",)
_SYMBOL = re.compile(r"^[A-Z0-9]{2,15}/USDT$")
_COID = re.compile(r"^[A-Za-z0-9]{8,28}$")

# per-user rate limits (in-process): reads and writes per 60s
READ_LIMIT, WRITE_LIMIT, WINDOW = 60, 20, 60.0
_HITS: dict[tuple[str, str], deque] = defaultdict(deque)
_RLOCK = threading.Lock()


class ServiceError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def reset_rate() -> None:
    with _RLOCK:
        _HITS.clear()


def rate(user_id: str, kind: str) -> None:
    limit = WRITE_LIMIT if kind == "write" else READ_LIMIT
    now = time.time()
    with _RLOCK:
        q = _HITS[(user_id, kind)]
        while q and now - q[0] > WINDOW:
            q.popleft()
        if len(q) >= limit:
            raise ServiceError("RATE_LIMITED", "操作太频繁，请一分钟后再试", 429)
        q.append(now)


def access(user: dict[str, Any]) -> tuple[bool, str]:
    s = store.settings()
    if not s["enabled"]:
        return False, "EXCHANGE_OFF"
    if s["allowlistOnly"] and str(user.get("id")) not in set(s["allowlist"]):
        return False, "EXCHANGE_NOT_ALLOWED"
    return True, ""


def require(user: dict[str, Any]) -> None:
    okay, code = access(user)
    if not okay:
        msg = "交易所接入功能暂未开放" if code == "EXCHANGE_OFF" else "你的账号暂未开通交易所接入（内测中）"
        raise ServiceError(code, msg, 403)


def user_cap(user_id: str) -> float:
    s = store.settings()
    v = s["userCaps"].get(user_id)
    return float(v) if v is not None else float(s["maxOrderUsdt"])


def futures_cap(user_id: str) -> float:
    s = store.settings()
    v = s["userFuturesCaps"].get(user_id)
    return float(v) if v is not None else float(s["maxFuturesUsdt"])


def public_config(user: Optional[dict[str, Any]]) -> dict[str, Any]:
    s = store.settings()
    okay, code = access(user) if user else (False, "AUTH_REQUIRED")
    return {
        "enabled": bool(s["enabled"]),
        "access": okay,
        "reason": code or None,
        "killSwitch": bool(s["killSwitch"]),
        "exchanges": list(EXCHANGES),
        "serverIps": SERVER_IPS,
        "maxOrderUsdt": user_cap(str(user["id"])) if user else float(s["maxOrderUsdt"]),
        "maxFuturesUsdt": futures_cap(str(user["id"])) if user else float(s["maxFuturesUsdt"]),
        "maxLeverage": int(s["maxLeverage"]),
        "futuresAck": bool(user and str(user["id"]) in s["futuresAcks"]),
        "cryptoReady": crypto.available(),
        "market": "spot",
        "markets": ["spot", "swap"],
        "quotes": list(ALLOWED_QUOTES),
    }


def _ex(exchange: str) -> str:
    e = (exchange or "").strip().lower()
    if e not in EXCHANGES:
        raise ServiceError("EXCHANGE_UNSUPPORTED", "只支持 Binance 和 OKX")
    return e


def _aad(user_id: str, exchange: str) -> str:
    return f"auu-exchange:{user_id}:{exchange}"


def bind(user_id: str, exchange: str, api_key: str, secret: str, password: str = "") -> dict[str, Any]:
    ex = _ex(exchange)
    api_key, secret, password = (api_key or "").strip(), (secret or "").strip(), (password or "").strip()
    if not (8 <= len(api_key) <= 200 and 8 <= len(secret) <= 200):
        raise ServiceError("BAD_KEY", "API Key 或 Secret 格式不对")
    if ex == "okx" and not (1 <= len(password) <= 64):
        raise ServiceError("BAD_KEY", "OKX 需要填写创建 API 时设置的 Passphrase")
    if not crypto.available():
        raise ServiceError("CRYPTO_UNAVAILABLE", "服务器加密密钥未配置，暂不能绑定", 503)
    creds = {"apiKey": api_key, "secret": secret, "password": password if ex == "okx" else ""}
    try:
        perms = check_permissions(ex, user_client(ex, creds))
    except ConnectorError as ce:
        store.audit(user_id, user_id, "bind_failed", {"exchange": ex, "code": ce.code})
        raise ServiceError(ce.code, ce.message, 400) from None
    if perms["withdraw"]:
        store.audit(user_id, user_id, "bind_rejected_withdraw", {"exchange": ex})
        raise ServiceError("KEY_HAS_WITHDRAW", "这个 API Key 开启了提现（或内部划转）权限，为了你的资金安全被拒绝。请在交易所关闭提现权限或新建一个只有读取+现货交易权限的 Key。")
    if not perms["read"]:
        raise ServiceError("KEY_NO_READ", "这个 API Key 没有读取权限")
    if not perms["ipRestricted"]:
        store.audit(user_id, user_id, "bind_rejected_no_ip", {"exchange": ex})
        raise ServiceError("KEY_NO_IP_WHITELIST", f"这个 API Key 没有设置 IP 白名单。请在交易所把 {SERVER_IPS['primary']} 和 {SERVER_IPS['standby']} 加入白名单后再绑定。")
    blob = crypto.seal(creds, _aad(user_id, ex))
    store.put_cred(user_id, ex, blob, crypto.mask(api_key), perms)
    store.audit(user_id, user_id, "bind", {"exchange": ex, "masked": crypto.mask(api_key), "perms": perms})
    return {"exchange": ex, "masked": crypto.mask(api_key), "perms": perms}


def unbind(user_id: str, exchange: str, actor: str = "") -> bool:
    ex = _ex(exchange)
    gone = store.del_cred(user_id, ex)
    store.audit(user_id, actor or user_id, "unbind", {"exchange": ex, "existed": gone})
    return gone


def _client(user_id: str, exchange: str, market: str = "spot"):
    ex = _ex(exchange)
    row = store.get_cred(user_id, ex)
    if not row:
        raise ServiceError("NOT_BOUND", "还没有绑定这个交易所", 404)
    try:
        creds = crypto.open_(row["blob"], _aad(user_id, ex))
    except crypto.CryptoUnavailable:
        raise ServiceError("CRYPTO_UNAVAILABLE", "服务器加密密钥未配置", 503) from None
    except Exception:  # noqa: BLE001
        raise ServiceError("CRED_UNREADABLE", "保存的密钥无法解密，请解绑后重新绑定", 500) from None
    return ex, row, user_client(ex, creds, market)


def _call(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except ConnectorError as ce:
        raise ServiceError(ce.code, ce.message, 502) from None
    except Exception as exc:  # noqa: BLE001
        ce = map_error(exc)
        raise ServiceError(ce.code, ce.message, 502) from None


def _sym(symbol: Optional[str], required: bool = False) -> Optional[str]:
    if not symbol:
        if required:
            raise ServiceError("BAD_SYMBOL", "请选择交易对")
        return None
    s = symbol.strip().upper().replace("-", "/")
    if "/" not in s:
        s = s + "/USDT"
    if not _SYMBOL.match(s):
        raise ServiceError("BAD_SYMBOL", "只支持 XXX/USDT 现货交易对")
    return s


def balances(user_id: str, exchange: str) -> list[dict[str, Any]]:
    _ex_, _row, c = _client(user_id, exchange)
    return norm_balance(_call(c.fetch_balance))


def open_orders(user_id: str, exchange: str, symbol: Optional[str]) -> list[dict[str, Any]]:
    ex, _row, c = _client(user_id, exchange)
    sym = _sym(symbol, required=False)
    if ex == "binance" and sym is None:
        c.options["warnOnFetchOpenOrdersWithoutSymbol"] = False
    return [norm_order(o) for o in _call(c.fetch_open_orders, sym)]


def history(user_id: str, exchange: str, symbol: Optional[str], limit: int = 50) -> list[dict[str, Any]]:
    ex, _row, c = _client(user_id, exchange)
    sym = _sym(symbol, required=(ex == "binance"))
    rows = _call(c.fetch_closed_orders, sym, None, int(limit))
    rows = sorted(rows, key=lambda o: o.get("timestamp") or 0, reverse=True)
    return [norm_order(o) for o in rows]


def place(user_id: str, exchange: str, body: dict[str, Any]) -> dict[str, Any]:
    ex = _ex(exchange)
    s = store.settings()
    if s["killSwitch"]:
        store.audit(user_id, user_id, "order_blocked_kill", {"exchange": ex})
        raise ServiceError("KILL_SWITCH", "管理员已开启紧急停止，暂时不能下新单（可以撤单）", 403)
    raw_coid = str(body.get("clientOrderId") or "")
    if not _COID.match(raw_coid):
        raise ServiceError("BAD_CLIENT_ORDER_ID", "clientOrderId 需为 8-28 位字母数字")
    coid = "auu" + raw_coid
    sym = _sym(body.get("symbol"), required=True)
    side = str(body.get("side") or "").lower()
    otype = str(body.get("type") or "").lower()
    if side not in {"buy", "sell"} or otype not in {"limit", "market"}:
        raise ServiceError("BAD_ORDER", "方向或类型不对")
    try:
        amount = float(body.get("amount") or 0)
        price = float(body.get("price") or 0) if otype == "limit" else 0.0
    except (TypeError, ValueError):
        raise ServiceError("BAD_ORDER", "数量或价格不对") from None
    if amount <= 0 or (otype == "limit" and price <= 0):
        raise ServiceError("BAD_ORDER", "数量和价格必须大于 0")

    prior = store.get_order(user_id, ex, coid)
    if prior is not None:
        return _resolve_prior(user_id, ex, coid, prior)

    _ex2, _row, c = _client(user_id, ex)
    ref = price
    if otype == "market":
        t = _call(c.fetch_ticker, sym)
        ref = float(t.get("ask") or t.get("last") or 0) if side == "buy" else float(t.get("bid") or t.get("last") or 0)
        if ref <= 0:
            raise ServiceError("NO_PRICE", "拿不到最新价格，请稍后再试", 502)
    notional = amount * ref
    cap = user_cap(user_id)
    if notional > cap:
        store.audit(user_id, user_id, "order_blocked_cap", {"exchange": ex, "symbol": sym, "notional": round(notional, 4), "cap": cap})
        raise ServiceError("ORDER_TOO_LARGE", f"单笔金额约 {notional:.2f} USDT，超过你的上限 {cap:.2f} USDT")

    request = {"symbol": sym, "side": side, "type": otype, "amount": amount, "price": price or None, "notionalEst": round(notional, 6)}
    if not store.claim_order(user_id, ex, coid, sym, request):
        return _resolve_prior(user_id, ex, coid, store.get_order(user_id, ex, coid) or {})
    store.audit(user_id, user_id, "order_submit", {"exchange": ex, "coid": coid, **request})
    params = {"clientOrderId": coid}
    try:
        o = c.create_order(sym, otype, side, amount, price if otype == "limit" else None, params)
    except Exception as exc:  # noqa: BLE001
        ce = map_error(exc)
        if ce.code in {"EXCHANGE_NETWORK", "EXCHANGE_ERROR"}:
            store.finish_order(user_id, ex, coid, "unknown", {"code": ce.code})
            store.audit(user_id, user_id, "order_unknown", {"exchange": ex, "coid": coid, "code": ce.code})
            raise ServiceError("ORDER_UNKNOWN", "下单结果未知（网络问题）。请稍后用同一笔订单重试查询，不会重复下单。", 502) from None
        store.finish_order(user_id, ex, coid, "rejected", {"code": ce.code, "message": ce.message})
        store.audit(user_id, user_id, "order_rejected", {"exchange": ex, "coid": coid, "code": ce.code})
        raise ServiceError(ce.code, ce.message, 400) from None
    out = norm_order(o)
    store.finish_order(user_id, ex, coid, "accepted", out)
    store.audit(user_id, user_id, "order_accepted", {"exchange": ex, "coid": coid, "orderId": out.get("id"), "status": out.get("status")})
    return {"order": out, "duplicate": False}


def _resolve_prior(user_id: str, ex: str, coid: str, prior: dict[str, Any]) -> dict[str, Any]:
    status = prior.get("status")
    if status == "accepted":
        return {"order": prior.get("result"), "duplicate": True}
    if status == "rejected":
        r = prior.get("result") or {}
        raise ServiceError(r.get("code") or "EXCHANGE_BAD_ORDER", r.get("message") or "这笔订单之前已被拒绝", 409)
    # pending / unknown: look it up on the exchange by clientOrderId, never resend
    _e, _row, c = _client(user_id, ex)
    try:
        o = c.fetch_order(None, prior.get("symbol"), {"clientOrderId": coid})
    except Exception as exc:  # noqa: BLE001
        if map_error(exc).code == "EXCHANGE_ORDER_NOT_FOUND":
            store.finish_order(user_id, ex, coid, "rejected", {"code": "NOT_PLACED", "message": "交易所没有这笔订单，请换一个新订单重新下单"})
            raise ServiceError("NOT_PLACED", "交易所没有这笔订单，请重新下单", 409) from None
        raise ServiceError("ORDER_UNKNOWN", "订单状态仍未知，请稍后再查", 502) from None
    out = norm_order(o)
    store.finish_order(user_id, ex, coid, "accepted", out)
    store.audit(user_id, user_id, "order_reconciled", {"exchange": ex, "coid": coid, "orderId": out.get("id")})
    return {"order": out, "duplicate": True}


def cancel(user_id: str, exchange: str, order_id: str, symbol: str) -> dict[str, Any]:
    ex, _row, c = _client(user_id, exchange)
    sym = _sym(symbol, required=True)
    oid = str(order_id or "").strip()
    if not oid or len(oid) > 64:
        raise ServiceError("BAD_ORDER", "订单号不对")
    o = _call(c.cancel_order, oid, sym)
    store.audit(user_id, user_id, "order_cancel", {"exchange": ex, "orderId": oid, "symbol": sym})
    return norm_order(o or {"id": oid, "symbol": sym, "status": "canceled"})
