"""Structured strategy-draft schema for the same-site studio.

Independent AUU schema. Drafts are data documents with a whitelisted kind
and numeric parameters. User-supplied code is rejected. Passing validation
marks a draft ``verified`` (schema + risk fields only). That is not an LLM
generation and not a claim that the rule is profitable.

Optional stop/take fields: omitted or JSON null means disabled. An omitted
``risk`` object and ``risk: {}`` have the same meaning. Costs default to
zero and must be disclosed as a gross, non-investment paper fraction.
"""
from __future__ import annotations

import math
import re
from typing import Any

SYMBOL_RE = re.compile(r"^[A-Z0-9]{1,12}$")
NAME_RE = re.compile(r"^[^\x00-\x1f]{1,80}$")
ALLOWED_TF = frozenset({"1h", "4h", "1d"})
TF_MS = {"1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}
FORBIDDEN_FIELDS = frozenset({
    "code", "script", "source", "source_code", "eval", "exec", "bytecode",
    "module", "import", "function", "handler",
})

KIND_SPECS: dict[str, dict[str, Any]] = {
    "sma_cross": {
        "params": {
            "fast": {"type": "int", "min": 2, "max": 50, "default": 10},
            "slow": {"type": "int", "min": 3, "max": 200, "default": 30},
        },
        "need": lambda p: p["slow"] > p["fast"],
        "need_bars": lambda p: int(p["slow"]) + 3,
        "channel": None,
    },
    "rsi_threshold": {
        "params": {
            "period": {"type": "int", "min": 2, "max": 50, "default": 14},
            "oversold": {"type": "int", "min": 5, "max": 45, "default": 30},
            "overbought": {"type": "int", "min": 55, "max": 95, "default": 70},
        },
        "need": lambda p: p["oversold"] < p["overbought"],
        "need_bars": lambda p: int(p["period"]) + 3,
        "channel": None,
    },
    "donchian_break": {
        "params": {
            "period": {"type": "int", "min": 5, "max": 100, "default": 20},
        },
        "need": lambda p: True,
        "need_bars": lambda p: int(p["period"]) + 3,
        "channel": "prior_bar_high_low",
    },
}

RISK_SPECS = {
    "max_weight": {"type": "float", "min": 0.01, "max": 1.0, "default": 0.2, "optional": False},
    "stop_loss_pct": {"type": "float", "min": 0.001, "max": 0.5, "default": None, "optional": True},
    "take_profit_pct": {"type": "float", "min": 0.001, "max": 2.0, "default": None, "optional": True},
    "long_only": {"type": "bool", "default": True, "optional": False},
    "fee_bps": {"type": "float", "min": 0.0, "max": 100.0, "default": 0.0, "optional": False},
    "slippage_bps": {"type": "float", "min": 0.0, "max": 100.0, "default": 0.0, "optional": False},
}


class SchemaError(ValueError):
    def __init__(self, code: str, message: str = "bad_body"):
        super().__init__(message)
        self.code = code
        self.message = message


def kinds_payload() -> list[dict[str, Any]]:
    rows = []
    for kind, spec in KIND_SPECS.items():
        rows.append({
            "id": kind,
            "params": {key: {k: v for k, v in meta.items()} for key, meta in spec["params"].items()},
            "risk": {key: {k: v for k, v in meta.items()} for key, meta in RISK_SPECS.items()},
            "timeframes": sorted(ALLOWED_TF),
            "generates_code": False,
            "llm": False,
            "execution": "signal_on_closed_bar_fill_next_open",
            "channel": spec.get("channel"),
        })
    return rows


def _require_str(value: Any, code: str) -> str:
    if not isinstance(value, str) or isinstance(value, bool):
        raise SchemaError(code, "bad_body")
    return value


def _num(value: Any, typ: str, lo: float, hi: float, field: str):
    if typ == "bool":
        if not isinstance(value, bool):
            raise SchemaError("BAD_FIELD", "bad_body")
        return value
    if isinstance(value, bool):
        raise SchemaError("BAD_FIELD", "bad_body")
    if typ == "int":
        if not isinstance(value, int):
            raise SchemaError("BAD_FIELD", "bad_body")
        if value < lo or value > hi:
            raise SchemaError("BAD_FIELD", "bad_body")
        return int(value)
    if not isinstance(value, (int, float)):
        raise SchemaError("BAD_FIELD", "bad_body")
    try:
        number = float(value)
    except (OverflowError, ValueError):
        raise SchemaError("BAD_FIELD", "bad_body") from None
    if not math.isfinite(number) or number < lo or number > hi:
        raise SchemaError("BAD_FIELD", "bad_body")
    return number


def _clean_params(kind: str, raw: Any) -> dict[str, Any]:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict) or isinstance(raw, bool):
        raise SchemaError("BAD_FIELD", "bad_body")
    spec = KIND_SPECS[kind]
    incoming = raw
    extra = set(incoming) - set(spec["params"])
    if extra:
        raise SchemaError("BAD_FIELD", "bad_body")
    out = {}
    for key, meta in spec["params"].items():
        value = incoming[key] if key in incoming else meta["default"]
        out[key] = _num(value, meta["type"], meta["min"], meta["max"], key)
    if not spec["need"](out):
        raise SchemaError("BAD_PARAMS", "bad_body")
    return out


def _clean_risk(raw: Any) -> dict[str, Any]:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict) or isinstance(raw, bool):
        raise SchemaError("BAD_FIELD", "bad_body")
    extra = set(raw) - set(RISK_SPECS)
    if extra:
        raise SchemaError("BAD_FIELD", "bad_body")
    out: dict[str, Any] = {}
    for key, meta in RISK_SPECS.items():
        if key not in raw or raw[key] is None:
            if meta.get("optional"):
                out[key] = None
            else:
                out[key] = meta["default"]
            continue
        value = raw[key]
        lo = meta.get("min", 0)
        hi = meta.get("max", 1)
        out[key] = _num(value, meta["type"], lo, hi, key)
    return out


def validate_strategy_body(body: Any) -> dict[str, Any]:
    if not isinstance(body, dict) or isinstance(body, bool):
        raise SchemaError("BAD_REQUEST", "bad_body")
    if set(body) & FORBIDDEN_FIELDS:
        raise SchemaError("CODE_FORBIDDEN", "user code is not accepted")
    name = _require_str(body.get("name"), "BAD_NAME").strip()
    if not NAME_RE.fullmatch(name):
        raise SchemaError("BAD_NAME", "bad_body")
    kind = body.get("kind")
    if not isinstance(kind, str) or kind not in KIND_SPECS:
        raise SchemaError("BAD_KIND", "bad_body")
    symbol = _require_str(body.get("symbol"), "BAD_SYMBOL").strip().upper()
    if not SYMBOL_RE.fullmatch(symbol):
        raise SchemaError("BAD_SYMBOL", "bad_body")
    tf = _require_str(body.get("timeframe"), "BAD_TIMEFRAME").strip()
    if tf not in ALLOWED_TF:
        raise SchemaError("BAD_TIMEFRAME", "bad_body")
    allowed_top = {"name", "kind", "symbol", "timeframe", "params", "risk"}
    if set(body) - allowed_top:
        raise SchemaError("BAD_FIELD", "bad_body")
    params = _clean_params(kind, body.get("params"))
    risk = _clean_risk(body.get("risk"))
    return {
        "name": name,
        "kind": kind,
        "symbol": symbol,
        "timeframe": tf,
        "params": params,
        "risk": risk,
        "status": "verified",
        "generates_code": False,
        "llm": False,
        "need_bars": int(KIND_SPECS[kind]["need_bars"](params)),
        "execution": "signal_on_closed_bar_fill_next_open",
        "channel": KIND_SPECS[kind].get("channel"),
    }


def validate_cost_override(body: dict[str, Any], risk: dict[str, Any]) -> dict[str, float]:
    """Apply optional per-request fee/slippage, still inside the schema range."""
    fee = risk.get("fee_bps") if risk.get("fee_bps") is not None else 0.0
    slip = risk.get("slippage_bps") if risk.get("slippage_bps") is not None else 0.0
    if "fee_bps" in body:
        fee = _num(body["fee_bps"], "float", 0.0, 100.0, "fee_bps")
    if "slippage_bps" in body:
        slip = _num(body["slippage_bps"], "float", 0.0, 100.0, "slippage_bps")
    return {"fee_bps": float(fee), "slippage_bps": float(slip)}
