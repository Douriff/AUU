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
DUP_WINDOW_MS = 3_000

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
    if s["allowlistOnly"] and not user.get("is_admin") and str(user.get("id")) not in set(s["allowlist"]):
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


# ---- trade gate: one rule set used by BOTH the availability endpoint (UI) and every order endpoint (server) ----
UNAVAILABLE_TEXT = {
    "AUTH_REQUIRED": "请先登录",
    "STANDBY": "备用节点不接受下单",
    "EXCHANGE_OFF": "交易所接入功能暂未开放（总开关关闭）",
    "EXCHANGE_NOT_ALLOWED": "你的账号暂未开通交易所接入（内测中）",
    "CRYPTO_UNAVAILABLE": "服务器加密密钥未配置",
    "NOT_BOUND": "还没有绑定这个交易所的 API Key",
    "KEY_NO_TRADE": "这个 API Key 没有现货交易权限，只能查看",
    "KEY_NO_FUTURES": "这个 API Key 没有合约交易权限（请开启合约权限后解绑重绑）",
    "KEY_HAS_WITHDRAW": "这个 API Key 带有提现权限，已禁止交易",
    "FUTURES_ACK_REQUIRED": "首次使用合约前，请先阅读并确认合约风险提示",
    "KILL_SWITCH": "管理员已开启紧急停止，暂时不能下新单（合约只允许平仓）",
}
_BLOCK_STATUS = {"AUTH_REQUIRED": 401, "STANDBY": 503, "CRYPTO_UNAVAILABLE": 503, "NOT_BOUND": 404}


def trade_block(user_id: str, ex: str, market: str = "spot", reduce_only: bool = False,
                user: Optional[dict[str, Any]] = None) -> Optional[tuple[str, str, int]]:
    """Why this user can NOT trade ``market`` on ``ex`` right now, or None. Checks that need no exchange call.

    ``reduce_only`` (futures close) skips only the kill switch: closing must stay possible in an emergency."""
    from app.role import is_standby

    if is_standby():
        code = "STANDBY"
    else:
        code = ""
        if user is not None:
            okay, why = access(user)
            code = "" if okay else why
        s = store.settings()
        row = store.get_cred(user_id, ex)
        if not code and not crypto.available():
            code = "CRYPTO_UNAVAILABLE"
        if not code and not row:
            code = "NOT_BOUND"
        if not code and row:
            perms = row.get("perms") or {}
            if perms.get("withdraw"):
                code = "KEY_HAS_WITHDRAW"
            elif market == "swap":
                if user_id not in s["futuresAcks"]:
                    code = "FUTURES_ACK_REQUIRED"
                elif not perms.get("futures"):
                    code = "KEY_NO_FUTURES"
            elif not perms.get("trade"):
                code = "KEY_NO_TRADE"
        if not code and s["killSwitch"] and not reduce_only:
            code = "KILL_SWITCH"
    if not code:
        return None
    return code, UNAVAILABLE_TEXT.get(code, "不可用"), _BLOCK_STATUS.get(code, 403)


def require_trade(user_id: str, ex: str, market: str = "spot", reduce_only: bool = False) -> None:
    b = trade_block(user_id, ex, market, reduce_only)
    if b:
        if b[0] == "KILL_SWITCH":
            store.audit(user_id, user_id, "order_blocked_kill", {"exchange": ex, "market": market})
        raise ServiceError(b[0], b[1], b[2])


def require_confirmed(body: dict[str, Any]) -> None:
    """Server-side second confirmation: the web UI sets ``confirmed`` only after its confirm dialog."""
    if body.get("confirmed") is not True:
        raise ServiceError("CONFIRM_REQUIRED", "下单需要二次确认（confirmed=true）", 400)


def require_market(c, sym: str, kind: str) -> None:
    """Refuse symbols the exchange does not list (or lists as inactive / wrong product type)."""
    mk = getattr(c, "markets", None)
    if not mk:
        return  # markets not loaded: the exchange itself will reject an unknown symbol
    m = mk.get(sym)
    if kind == "swap":
        good = bool(m) and bool(m.get("swap")) and m.get("linear") is not False and m.get("quote", "USDT") == "USDT"
    else:
        good = bool(m) and bool(m.get("spot")) and m.get("quote", "USDT") == "USDT"
    if not good or m.get("active") is False:
        raise ServiceError("SYMBOL_UNSUPPORTED", "这个交易所不支持该品种（或暂不可交易）", 400)


def availability(user: Optional[dict[str, Any]]) -> dict[str, Any]:
    """Per exchange x market: can the user open / close now, and if not the reason code + text (no exchange calls)."""
    if user is None:
        return {"enabled": False, "killSwitch": False, "exchanges": {}, "reason": "AUTH_REQUIRED"}
    s = store.settings()
    uid = str(user["id"])
    out: dict[str, Any] = {}
    for ex in EXCHANGES:
        row: dict[str, Any] = {"bound": store.get_cred(uid, ex) is not None}
        for market in ("spot", "swap"):
            op = trade_block(uid, ex, market, False, user)
            cl = trade_block(uid, ex, market, market == "swap", user)
            pack = lambda b: {"available": b is None, "reason": b[0] if b else None, "message": b[1] if b else None}  # noqa: E731
            row[market] = {"open": pack(op), "close": pack(cl)}
        out[ex] = row
    return {"enabled": bool(s["enabled"]), "killSwitch": bool(s["killSwitch"]), "exchanges": out}


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
    require_confirmed(body)
    require_trade(user_id, ex, "spot")
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
    require_market(c, sym, "spot")
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
    if store.recent_same(user_id, ex, request, DUP_WINDOW_MS):  # a double click with a different id still must not double-order
        raise ServiceError("DUPLICATE_ORDER", "相同的订单刚刚提交过，请确认后再下", 409)
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


def quote(user_id: str, exchange: str, symbol: str, market: str = "spot") -> dict[str, Any]:
    """Best bid / ask / last for the order-confirmation dialog (estimated fill price). Read-only."""
    from app.exchange import futures as fut

    mk = "swap" if market == "swap" else "spot"
    sym = fut.fsym(symbol) if mk == "swap" else _sym(symbol, required=True)
    ex, _row, c = _client(user_id, exchange, mk)
    require_market(c, sym, mk)
    t = _call(c.fetch_ticker, sym)
    return {"exchange": ex, "market": mk, "symbol": sym, "bid": t.get("bid"), "ask": t.get("ask"), "last": t.get("last"),
            "contractSize": fut._contract_size(c, sym) if mk == "swap" else None, "ts": t.get("timestamp")}


def supported(user_id: str, exchange: str, symbols: list[str], market: str = "spot") -> dict[str, bool]:
    """Which of ``symbols`` (bases like "BTC" or full pairs) this exchange lists as active USDT products. No orders, no key use
    beyond loading the shared public market list; lets the UI show unsupported coins as unavailable up front."""
    from app.exchange import futures as fut

    mk = "swap" if market == "swap" else "spot"
    _ex_, _row, c = _client(user_id, exchange, mk)
    out: dict[str, bool] = {}
    for raw in list(symbols)[:200]:
        try:
            sym = fut.fsym(raw) if mk == "swap" else _sym(raw, required=True)
            require_market(c, sym, mk)
            out[str(raw)] = True
        except ServiceError:
            out[str(raw)] = False
    return out
