"""USDT-margined perpetuals (Binance USD-M, OKX SWAP) for user keys.

Rules: first-time risk acknowledgement required; leverage <= admin maxLeverage (default 5);
per-order notional cap separate from spot; kill switch blocks opening orders (reduce-only
closes stay allowed); admin can close all positions of a user. Idempotency shares the spot
orders table under exchange key "<ex>:swap".
"""
from __future__ import annotations

import re
from typing import Any, Optional

from app.exchange import store
from app.exchange.connector import map_error, norm_order, norm_position
from app.exchange.service import ServiceError, _call, _client, _ex, futures_cap

_SYM = re.compile(r"^[A-Z0-9]{2,15}/USDT:USDT$")
_COID = re.compile(r"^[A-Za-z0-9]{8,28}$")
MARGIN_MODES = ("cross", "isolated")


def fsym(symbol: Optional[str], required: bool = True) -> Optional[str]:
    if not symbol:
        if required:
            raise ServiceError("BAD_SYMBOL", "请选择合约")
        return None
    s = symbol.strip().upper().replace("-", "/")
    if s.endswith("-SWAP"):
        s = s[:-5]
    if "/" not in s:
        s += "/USDT"
    if ":" not in s:
        s += ":USDT"
    if not _SYM.match(s):
        raise ServiceError("BAD_SYMBOL", "只支持 USDT 本位永续合约，例如 BTC/USDT:USDT")
    return s


def ack(user_id: str) -> None:
    s = store.settings()
    acks = dict(s["futuresAcks"])
    acks[user_id] = store.now_ms()
    store.set_setting("futuresAcks", acks)
    store.audit(user_id, user_id, "futures_ack", {})


def _need_ack(user_id: str) -> None:
    if user_id not in store.settings()["futuresAcks"]:
        raise ServiceError("FUTURES_ACK_REQUIRED", "首次使用合约前，请先阅读并确认合约风险提示", 403)


def _fc(user_id: str, exchange: str):
    _need_ack(user_id)
    return _client(user_id, exchange, "swap")


def positions(user_id: str, exchange: str) -> list[dict[str, Any]]:
    _e, _r, c = _fc(user_id, exchange)
    rows = _call(c.fetch_positions)
    return [norm_position(p) for p in rows if float(p.get("contracts") or 0) != 0]


def open_orders(user_id: str, exchange: str, symbol: Optional[str]) -> list[dict[str, Any]]:
    ex, _r, c = _fc(user_id, exchange)
    sym = fsym(symbol, required=False)
    if ex == "binance" and sym is None:
        c.options["warnOnFetchOpenOrdersWithoutSymbol"] = False
    return [norm_order(o) for o in _call(c.fetch_open_orders, sym)]


def funding(user_id: str, exchange: str, symbol: str) -> dict[str, Any]:
    _e, _r, c = _fc(user_id, exchange)
    sym = fsym(symbol)
    f = _call(c.fetch_funding_rate, sym)
    return {"symbol": sym, "fundingRate": f.get("fundingRate"), "nextFundingTime": f.get("fundingTimestamp") or f.get("nextFundingTimestamp"),
            "markPrice": f.get("markPrice"), "indexPrice": f.get("indexPrice"), "interval": f.get("interval")}


def set_leverage(user_id: str, exchange: str, symbol: str, leverage: int, margin_mode: str) -> dict[str, Any]:
    ex, _r, c = _fc(user_id, exchange)
    sym = fsym(symbol)
    mx = int(store.settings()["maxLeverage"])
    lev = int(leverage)
    if not (1 <= lev <= mx):
        raise ServiceError("LEVERAGE_TOO_HIGH", f"杠杆只能设置 1–{mx} 倍")
    mode = (margin_mode or "").lower()
    if mode not in MARGIN_MODES:
        raise ServiceError("BAD_MARGIN_MODE", "保证金模式只能是全仓或逐仓")
    try:
        if ex == "okx":
            c.set_margin_mode(mode, sym, {"lever": lev})
            c.set_leverage(lev, sym, {"mgnMode": mode})
        else:
            try:
                c.set_margin_mode(mode, sym)
            except Exception as exc:  # noqa: BLE001 - binance errors when mode is unchanged
                if "No need to change" not in str(exc) and "-4046" not in str(exc):
                    raise
            c.set_leverage(lev, sym)
    except Exception as exc:  # noqa: BLE001
        ce = map_error(exc)
        raise ServiceError(ce.code, ce.message, 502) from None
    store.audit(user_id, user_id, "futures_leverage", {"exchange": ex, "symbol": sym, "leverage": lev, "marginMode": mode})
    return {"symbol": sym, "leverage": lev, "marginMode": mode}


def _contract_size(c, sym: str) -> float:
    try:
        return float(c.market(sym).get("contractSize") or 1)
    except Exception:  # noqa: BLE001
        return 1.0


def place(user_id: str, exchange: str, body: dict[str, Any]) -> dict[str, Any]:
    ex = _ex(exchange)
    key = f"{ex}:swap"
    reduce_only = bool(body.get("reduceOnly"))
    s = store.settings()
    if s["killSwitch"] and not reduce_only:
        store.audit(user_id, user_id, "futures_blocked_kill", {"exchange": ex})
        raise ServiceError("KILL_SWITCH", "管理员已开启紧急停止：不能开新仓，只能平仓/撤单", 403)
    raw = str(body.get("clientOrderId") or "")
    if not _COID.match(raw):
        raise ServiceError("BAD_CLIENT_ORDER_ID", "clientOrderId 需为 8-28 位字母数字")
    coid = "auu" + raw
    sym = fsym(body.get("symbol"))
    side = str(body.get("side") or "").lower()
    otype = str(body.get("type") or "").lower()
    if side not in {"buy", "sell"} or otype not in {"limit", "market"}:
        raise ServiceError("BAD_ORDER", "方向或类型不对")
    try:
        amount = float(body.get("amount") or 0)
        price = float(body.get("price") or 0) if otype == "limit" else 0.0
        tp = float(body.get("takeProfit") or 0)
        sl = float(body.get("stopLoss") or 0)
    except (TypeError, ValueError):
        raise ServiceError("BAD_ORDER", "数量或价格不对") from None
    if amount <= 0 or (otype == "limit" and price <= 0) or tp < 0 or sl < 0:
        raise ServiceError("BAD_ORDER", "数量和价格必须大于 0")

    prior = store.get_order(user_id, key, coid)
    if prior is not None:
        return _resolve_prior_swap(user_id, ex, key, coid, prior)

    _e, _r, c = _fc(user_id, ex)
    ref = price
    if otype == "market" or not ref:
        t = _call(c.fetch_ticker, sym)
        ref = float(t.get("ask") or t.get("last") or 0) if side == "buy" else float(t.get("bid") or t.get("last") or 0)
        if ref <= 0:
            raise ServiceError("NO_PRICE", "拿不到最新价格，请稍后再试", 502)
    notional = amount * _contract_size(c, sym) * ref
    if side == "buy" and ((tp and tp <= ref) or (sl and sl >= ref)) and not reduce_only:
        raise ServiceError("BAD_TPSL", "做多时止盈价要高于现价、止损价要低于现价")
    if side == "sell" and ((tp and tp >= ref) or (sl and sl <= ref)) and not reduce_only:
        raise ServiceError("BAD_TPSL", "做空时止盈价要低于现价、止损价要高于现价")
    cap = futures_cap(user_id)
    if not reduce_only and notional > cap:
        store.audit(user_id, user_id, "futures_blocked_cap", {"exchange": ex, "symbol": sym, "notional": round(notional, 4), "cap": cap})
        raise ServiceError("ORDER_TOO_LARGE", f"合约单笔名义价值约 {notional:.2f} USDT，超过你的上限 {cap:.2f} USDT")

    request = {"symbol": sym, "side": side, "type": otype, "amount": amount, "price": price or None, "reduceOnly": reduce_only,
               "takeProfit": tp or None, "stopLoss": sl or None, "notionalEst": round(notional, 6)}
    if not store.claim_order(user_id, key, coid, sym, request):
        return _resolve_prior_swap(user_id, ex, key, coid, store.get_order(user_id, key, coid) or {})
    store.audit(user_id, user_id, "futures_submit", {"exchange": ex, "coid": coid, **request})
    params: dict[str, Any] = {"clientOrderId": coid}
    if reduce_only:
        params["reduceOnly"] = True
    try:
        o = c.create_order(sym, otype, side, amount, price if otype == "limit" else None, params)
    except Exception as exc:  # noqa: BLE001
        ce = map_error(exc)
        if ce.code in {"EXCHANGE_NETWORK", "EXCHANGE_ERROR"}:
            store.finish_order(user_id, key, coid, "unknown", {"code": ce.code})
            raise ServiceError("ORDER_UNKNOWN", "下单结果未知（网络问题）。请稍后用同一笔订单重试查询，不会重复下单。", 502) from None
        store.finish_order(user_id, key, coid, "rejected", {"code": ce.code, "message": ce.message})
        store.audit(user_id, user_id, "futures_rejected", {"exchange": ex, "coid": coid, "code": ce.code})
        raise ServiceError(ce.code, ce.message, 400) from None
    out = norm_order(o)
    store.finish_order(user_id, key, coid, "accepted", out)
    store.audit(user_id, user_id, "futures_accepted", {"exchange": ex, "coid": coid, "orderId": out.get("id")})
    # TP / SL as separate reduce-only conditional orders on the opposite side
    close_side = "sell" if side == "buy" else "buy"
    extras: list[dict[str, Any]] = []
    for label, px, pkey in (("takeProfit", tp, "takeProfitPrice"), ("stopLoss", sl, "stopLossPrice")):
        if not px or reduce_only:
            continue
        try:
            co = c.create_order(sym, "market", close_side, amount, None, {"reduceOnly": True, pkey: px, "clientOrderId": f"{coid[:28]}{label[0].upper()}"[:32]})
            extras.append({"kind": label, "ok": True, "order": norm_order(co)})
            store.audit(user_id, user_id, "futures_tpsl", {"exchange": ex, "coid": coid, "kind": label, "price": px})
        except Exception as exc:  # noqa: BLE001
            ce = map_error(exc)
            extras.append({"kind": label, "ok": False, "message": ce.message})
            store.audit(user_id, user_id, "futures_tpsl_failed", {"exchange": ex, "coid": coid, "kind": label, "code": ce.code})
    return {"order": out, "duplicate": False, "tpsl": extras}


def _resolve_prior_swap(user_id: str, ex: str, key: str, coid: str, prior: dict[str, Any]) -> dict[str, Any]:
    status = prior.get("status")
    if status == "accepted":
        return {"order": prior.get("result"), "duplicate": True, "tpsl": []}
    if status == "rejected":
        r = prior.get("result") or {}
        raise ServiceError(r.get("code") or "EXCHANGE_BAD_ORDER", r.get("message") or "这笔订单之前已被拒绝", 409)
    _e, _r, c = _fc(user_id, ex)
    try:
        o = c.fetch_order(None, prior.get("symbol"), {"clientOrderId": coid})
    except Exception as exc:  # noqa: BLE001
        if map_error(exc).code == "EXCHANGE_ORDER_NOT_FOUND":
            store.finish_order(user_id, key, coid, "rejected", {"code": "NOT_PLACED", "message": "交易所没有这笔订单，请重新下单"})
            raise ServiceError("NOT_PLACED", "交易所没有这笔订单，请重新下单", 409) from None
        raise ServiceError("ORDER_UNKNOWN", "订单状态仍未知，请稍后再查", 502) from None
    out = norm_order(o)
    store.finish_order(user_id, key, coid, "accepted", out)
    return {"order": out, "duplicate": True, "tpsl": []}


def cancel(user_id: str, exchange: str, order_id: str, symbol: str) -> dict[str, Any]:
    ex, _r, c = _fc(user_id, exchange)
    sym = fsym(symbol)
    oid = str(order_id or "").strip()
    if not oid or len(oid) > 64:
        raise ServiceError("BAD_ORDER", "订单号不对")
    o = _call(c.cancel_order, oid, sym)
    store.audit(user_id, user_id, "futures_cancel", {"exchange": ex, "orderId": oid, "symbol": sym})
    return norm_order(o or {"id": oid, "symbol": sym, "status": "canceled"})


def _close_one(c, p: dict[str, Any]) -> dict[str, Any]:
    sym = p["symbol"]
    qty = abs(float(p.get("contracts") or 0))
    side = "sell" if (p.get("side") or "long") == "long" else "buy"
    import secrets

    params: dict[str, Any] = {"reduceOnly": True, "clientOrderId": "auuX" + secrets.token_hex(10)}
    if p.get("marginMode"):
        params["marginMode"] = p["marginMode"]
    o = c.create_order(sym, "market", side, qty, None, params)
    return norm_order(o)


def close_position(user_id: str, exchange: str, symbol: str, actor: str = "") -> dict[str, Any]:
    ex, _r, c = _fc(user_id, exchange)
    sym = fsym(symbol)
    rows = [p for p in _call(c.fetch_positions, [sym]) if float(p.get("contracts") or 0) != 0 and p.get("symbol") == sym]
    if not rows:
        raise ServiceError("NO_POSITION", "没有这个合约的持仓", 404)
    out = [_call(_close_one, c, p) for p in rows]
    store.audit(user_id, actor or user_id, "futures_close", {"exchange": ex, "symbol": sym, "n": len(out)})
    return {"closed": out}


def close_all(user_id: str, actor: str) -> dict[str, Any]:
    """Admin emergency: cancel open futures orders and market-close every position (all exchanges)."""
    report: list[dict[str, Any]] = []
    for row in store.list_creds(user_id):
        ex = row["exchange"]
        try:
            _e, _r, c = _client(user_id, ex, "swap")
            pos = [p for p in c.fetch_positions() if float(p.get("contracts") or 0) != 0]
            for sym in sorted({p["symbol"] for p in pos}):
                try:
                    c.cancel_all_orders(sym)
                except Exception:  # noqa: BLE001
                    pass
            for p in pos:
                try:
                    report.append({"exchange": ex, "symbol": p["symbol"], "ok": True, "order": _close_one(c, p)})
                except Exception as exc:  # noqa: BLE001
                    report.append({"exchange": ex, "symbol": p["symbol"], "ok": False, "message": map_error(exc).message})
            if not pos:
                report.append({"exchange": ex, "ok": True, "message": "无持仓"})
        except ServiceError as e:
            report.append({"exchange": ex, "ok": False, "message": e.message})
        except Exception as exc:  # noqa: BLE001
            report.append({"exchange": ex, "ok": False, "message": map_error(exc).message})
    store.audit(user_id, actor, "admin_close_all", {"report": [{k: v for k, v in r.items() if k != "order"} for r in report]})
    return {"report": report}
