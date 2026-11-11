"""Session execution draft. Nothing here is stored or armed.

Blank numbers stay blank. There is no implied live balance, leverage, or stop.
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional

_SYMBOL = re.compile(r"^[A-Z0-9]{1,12}$")
_SECRET = {"api_key", "apikey", "secret", "token", "password", "exchange_key", "passphrase", "private_key"}
_INTENTS = {"paper", "live"}
_CONFIRMS = {"each_order", "auto_within_limits"}
_MARKETS = {"spot", "perp"}
_DIRECTIONS = {"long_only", "long_short"}


def _blank(value: Any) -> bool:
    return value is None or value == ""


def _number(value: Any, lo: float, hi: float) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("bad_number")
    number = float(value)
    if number != number or number in {float("inf"), float("-inf")} or not lo <= number <= hi:
        raise ValueError("bad_number")
    return number


def _optional_number(body: dict[str, Any], key: str, lo: float, hi: float) -> Optional[float]:
    if key not in body or _blank(body[key]):
        return None
    return _number(body[key], lo, hi)


def _choice(body: dict[str, Any], key: str, allowed: set[str]) -> Optional[str]:
    if key not in body or _blank(body[key]):
        return None
    value = body[key]
    if not isinstance(value, str) or value not in allowed:
        raise ValueError("bad_choice")
    return value


def validate_draft(body: Any) -> dict[str, Any]:
    if not isinstance(body, dict) or len(json.dumps(body, default=str)) > 8000:
        return _refused("bad_body")
    if _SECRET.intersection(body):
        return _refused("secret_rejected")
    try:
        symbols = _symbols(body.get("symbols"))
        draft = {
            "intent": _choice(body, "intent", _INTENTS),
            "confirm": _choice(body, "confirm", _CONFIRMS),
            "symbols": symbols,
            "market": _choice(body, "market", _MARKETS),
            "direction": _choice(body, "direction", _DIRECTIONS),
            "capital_limit": _optional_number(body, "capital_limit", 0.01, 1_000_000_000),
            "capital_ccy": _ccy(body.get("capital_ccy")),
            "order_limit": _optional_number(body, "order_limit", 0.01, 1_000_000_000),
            "leverage": _optional_number(body, "leverage", 1, 50),
            "hard_stop_fraction": _optional_number(body, "hard_stop_fraction", 0.001, 0.5),
            "daily_loss_limit": _optional_number(body, "daily_loss_limit", 0.01, 1_000_000_000),
            "drawdown_limit": _optional_number(body, "drawdown_limit", 0.001, 0.95),
            "max_positions": _optional_int(body, "max_positions", 1, 50),
            "slippage_bps": _optional_number(body, "slippage_bps", 0, 500),
            "stale_after_sec": _optional_int(body, "stale_after_sec", 1, 86_400),
            "kill_switch_preference": body.get("kill_switch_preference") is True,
        }
    except ValueError as exc:
        return _refused(str(exc) or "bad_body")
    return {
        "ok": True,
        "draft": draft,
        "armed": False,
        "live_enabled": False,
        "channel": "not_connected",
        "persistence": "session_draft",
        "saved": False,
        "can_enable": False,
        "exchange_keys_collected": False,
    }


def _refused(reason: str) -> dict[str, Any]:
    return {
        "ok": False,
        "reason": reason,
        "armed": False,
        "live_enabled": False,
        "channel": "not_connected",
        "saved": False,
        "can_enable": False,
    }


def _symbols(value: Any) -> list[str]:
    if _blank(value):
        return []
    if isinstance(value, str):
        parts = [item.strip().upper() for item in value.split(",") if item.strip()]
    elif isinstance(value, list):
        parts = [str(item).strip().upper() for item in value if str(item).strip()]
    else:
        raise ValueError("bad_symbols")
    if len(parts) > 40 or any(_SYMBOL.fullmatch(item) is None for item in parts):
        raise ValueError("bad_symbols")
    return list(dict.fromkeys(parts))


def _ccy(value: Any) -> Optional[str]:
    if _blank(value):
        return None
    if not isinstance(value, str) or value not in {"USDT", "USD"}:
        raise ValueError("bad_ccy")
    return value


def _optional_int(body: dict[str, Any], key: str, lo: int, hi: int) -> Optional[int]:
    if key not in body or _blank(body[key]):
        return None
    value = body[key]
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        raise ValueError("bad_number")
    return value
