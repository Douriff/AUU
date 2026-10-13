"""ccxt connectors for user keys (Binance / OKX spot only).

`make_client` is the single place a ccxt client with credentials is built (tests patch it).
Markets are loaded once per exchange with a keyless client and shared into user clients.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Optional

EXCHANGES = ("binance", "okx")
_MARKETS: dict[str, tuple[float, Any, Any]] = {}
_MLOCK = threading.Lock()
MARKETS_TTL = 6 * 3600


class ConnectorError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


MARKET_TYPES = {"spot": {"binance": "spot", "okx": "spot"}, "swap": {"binance": "future", "okx": "swap"}}


def make_client(exchange: str, creds: Optional[dict[str, str]] = None, market: str = "spot"):
    import ccxt  # MIT

    dtype = MARKET_TYPES.get(market, MARKET_TYPES["spot"])[exchange]
    opts: dict[str, Any] = {"enableRateLimit": True, "timeout": 15000, "options": {"defaultType": dtype}}
    if creds:
        opts["apiKey"] = creds["apiKey"]
        opts["secret"] = creds["secret"]
        if creds.get("password"):
            opts["password"] = creds["password"]
    return getattr(ccxt, exchange)(opts)


def _shared_markets(exchange: str, market: str = "spot"):
    with _MLOCK:
        key = f"{exchange}:{market}"
        hit = _MARKETS.get(key)
        if hit and time.time() - hit[0] < MARKETS_TTL:
            return hit[1], hit[2]
        c = make_client(exchange, None, market)
        c.load_markets()
        _MARKETS[key] = (time.time(), c.markets, c.currencies)
        return c.markets, c.currencies


def user_client(exchange: str, creds: dict[str, str], market: str = "spot"):
    c = make_client(exchange, creds, market)
    try:
        markets, currencies = _shared_markets(exchange, market)
        c.set_markets(markets, currencies)
    except Exception:  # noqa: BLE001 - fall back to the client's own lazy load
        pass
    return c


def map_error(exc: Exception) -> ConnectorError:
    name = type(exc).__name__
    text = str(exc)[:300]
    if name in {"AuthenticationError", "PermissionDenied", "AccountSuspended"}:
        return ConnectorError("EXCHANGE_AUTH", "交易所拒绝了这个 API Key（无效、权限不足或 IP 不在白名单）")
    if name in {"InsufficientFunds"}:
        return ConnectorError("EXCHANGE_FUNDS", "余额不足")
    if name in {"InvalidOrder", "BadSymbol", "BadRequest", "ArgumentsRequired"}:
        return ConnectorError("EXCHANGE_BAD_ORDER", "交易所拒绝订单：" + text)
    if name in {"OrderNotFound"}:
        return ConnectorError("EXCHANGE_ORDER_NOT_FOUND", "订单不存在")
    if name in {"RateLimitExceeded", "DDoSProtection"}:
        return ConnectorError("EXCHANGE_RATE_LIMIT", "交易所限频，请稍后再试")
    if name in {"RequestTimeout", "NetworkError", "ExchangeNotAvailable", "OnMaintenance"}:
        return ConnectorError("EXCHANGE_NETWORK", "连接交易所超时或不可用")
    return ConnectorError("EXCHANGE_ERROR", "交易所返回错误")


def check_permissions(exchange: str, client) -> dict[str, Any]:
    """Return {read, trade, withdraw, ipRestricted}. Raises ConnectorError on auth failure."""
    try:
        if exchange == "binance":
            r = client.sapiGetAccountApiRestrictions()
            truthy = lambda v: v is True or str(v).lower() == "true"  # noqa: E731
            return {
                "read": truthy(r.get("enableReading")),
                "trade": truthy(r.get("enableSpotAndMarginTrading")),
                "futures": truthy(r.get("enableFutures")),
                "withdraw": truthy(r.get("enableWithdrawals")) or truthy(r.get("enableInternalTransfer")) or truthy(r.get("permitsUniversalTransfer")),
                "ipRestricted": truthy(r.get("ipRestrict")),
            }
        if exchange == "okx":
            r = client.privateGetAccountConfig()
            row = (r.get("data") or [{}])[0]
            perm = {p.strip() for p in str(row.get("perm") or "").split(",") if p.strip()}
            return {
                "read": "read_only" in perm or bool(perm),
                "trade": "trade" in perm,
                "futures": "trade" in perm,
                "withdraw": "withdraw" in perm,
                "ipRestricted": bool(str(row.get("ip") or "").strip()),
            }
    except ConnectorError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise map_error(exc) from exc
    raise ConnectorError("EXCHANGE_UNSUPPORTED", "不支持的交易所")


def norm_order(o: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": o.get("id"),
        "clientOrderId": o.get("clientOrderId"),
        "symbol": o.get("symbol"),
        "side": o.get("side"),
        "type": o.get("type"),
        "price": o.get("price"),
        "average": o.get("average"),
        "amount": o.get("amount"),
        "filled": o.get("filled"),
        "cost": o.get("cost"),
        "status": o.get("status"),
        "timestamp": o.get("timestamp"),
    }


def norm_balance(b: dict[str, Any]) -> list[dict[str, Any]]:
    total = b.get("total") or {}
    free = b.get("free") or {}
    used = b.get("used") or {}
    out = []
    for asset, t in total.items():
        try:
            tv = float(t or 0)
        except (TypeError, ValueError):
            continue
        if tv <= 0:
            continue
        out.append({"asset": asset, "total": tv, "free": float(free.get(asset) or 0), "used": float(used.get(asset) or 0)})
    out.sort(key=lambda r: r["asset"])
    return out


def norm_position(p: dict[str, Any]) -> dict[str, Any]:
    return {
        "symbol": p.get("symbol"),
        "side": p.get("side"),
        "contracts": p.get("contracts"),
        "contractSize": p.get("contractSize"),
        "notional": p.get("notional"),
        "entryPrice": p.get("entryPrice"),
        "markPrice": p.get("markPrice"),
        "liquidationPrice": p.get("liquidationPrice"),
        "unrealizedPnl": p.get("unrealizedPnl"),
        "leverage": p.get("leverage"),
        "marginMode": p.get("marginMode"),
        "initialMargin": p.get("initialMargin"),
        "percentage": p.get("percentage"),
    }
