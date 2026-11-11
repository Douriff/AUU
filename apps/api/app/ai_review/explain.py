"""Deterministic rule replay. Numbers come from the stored row or the frozen rule.

This module does not call a model and does not invent a stop, a target, or a
causal story. If the previous bar or the parameter hash is missing, the
replay stops.
"""
from __future__ import annotations

from typing import Any, Optional

from app.ai_review.schema import SYMBOL

_SIDES = {"long", "short", "flat"}


def explain_bar(
    identity: dict[str, Any],
    bar: Optional[dict],
    prev: Optional[dict],
    *,
    gap: bool = False,
) -> dict[str, Any]:
    base = {
        "label": "rules_not_ai",
        "ai_generated": False,
        "reconstructed": "no",
        "strategy_id": identity["id"],
        "symbol": None if not bar else bar.get("symbol"),
        "asof_ts": None if not bar else bar.get("asof_ts"),
        "recorded_at": None if not bar else bar.get("recorded_at"),
        "timeframe": identity.get("timeframe"),
        "clock": "asof_ts_is_bar_open",
        "side": None if not bar else bar.get("side"),
        "score": None if not bar else bar.get("score"),
        "action": None if not bar else bar.get("action"),
        "conditions": [],
        "invalidation": [{"id": "stop_loss", "status": "not_in_rule"}],
        "risks": [
            {"id": "record_only"},
            {"id": "no_stop_in_rule"},
            {"id": "score_is_not_a_causal_claim"},
        ],
        "reason": None,
    }
    if bar is None:
        base["reason"] = "no_stored_bar"
        return base
    if not SYMBOL.fullmatch(str(bar.get("symbol") or "")):
        base["reason"] = "symbol_not_usable"
        base["symbol"] = None
        return base
    if bar.get("side") not in _SIDES or not isinstance(bar.get("score"), (int, float)):
        base["reason"] = "stored_row_unusable"
        return base
    if identity.get("params_hash") and bar.get("params_hash") not in {None, identity["params_hash"]}:
        base["reason"] = "params_hash_mismatch"
        base["recorded"] = _recorded(bar)
        return base
    if identity["id"] != "auu_signal_v0" and identity.get("params_hash") and not bar.get("params_hash"):
        base["reason"] = "params_hash_missing"
        base["recorded"] = _recorded(bar)
        return base

    score = float(bar["score"])
    enter = identity["hold"].get("enter")
    exit_th = identity["hold"].get("exit")
    if enter is None or exit_th is None:
        base["reason"] = "rule_has_no_signal_threshold"
        base["recorded"] = _recorded(bar)
        return base

    conditions = [
        _cmp("score_ge_enter", score, ">=", float(enter)),
        _cmp("score_le_neg_enter", score, "<=", -float(enter)),
        _cmp("score_lt_exit", score, "<", float(exit_th)),
        _cmp("score_gt_neg_exit", score, ">", -float(exit_th)),
    ]
    factor_status = _factor_status(identity, bar, score)
    conditions.append(factor_status)
    conditions.extend(_tag_checks(identity, bar))

    prev_side = None if prev is None else prev.get("side")
    if gap or prev is None or prev_side not in _SIDES:
        conditions.append({"id": "previous_side", "status": "unknown", "observed": None, "source": "stored_bar"})
        base["conditions"] = conditions
        base["reconstructed"] = "partial"
        base["reason"] = "previous_side_unknown" if not gap else "gap_before_bar"
        base["recorded"] = _recorded(bar)
        base["invalidation"] = _invalidation(bar.get("side"), score, float(exit_th), float(enter), identity["long_only"])
        return base

    expected = _side(str(prev_side), score, float(enter), float(exit_th), bool(identity["long_only"]))
    match = expected == bar.get("side")
    conditions.append(
        {
            "id": "replay_matches_stored_side",
            "status": "met" if match else "unmet",
            "observed": bar.get("side"),
            "expected": expected,
            "source": "frozen_rule",
        }
    )
    expected_action = _action(str(prev_side), expected)
    stored_action = bar.get("action")
    action_ok = stored_action == expected_action or (
        expected_action == "none" and stored_action in {"none", "hold"}
    )
    conditions.append(
        {
            "id": "replay_matches_stored_action",
            "status": "met" if action_ok else "unmet",
            "observed": stored_action,
            "expected": expected_action if expected_action != "none" else "none_or_hold",
            "source": "frozen_rule",
        }
    )
    base["conditions"] = conditions
    base["recorded"] = _recorded(bar)
    base["invalidation"] = _invalidation(bar.get("side"), score, float(exit_th), float(enter), identity["long_only"])
    if not match or not action_ok:
        base["reconstructed"] = "partial"
        base["reason"] = "replay_mismatch"
        _binding(base, identity)
        return base
    _binding(base, identity)
    if not identity.get("params_hash"):
        base["reconstructed"] = "partial"
        base["reason"] = "params_hash_not_stored"
        return base
    base["reconstructed"] = "yes"
    base["reason"] = None
    return base


def _binding(base: dict[str, Any], identity: dict[str, Any]) -> None:
    if identity.get("params_hash"):
        base["params_hash_verified"] = True
        base["params_binding"] = "params_sha256_16"
    else:
        base["params_hash_verified"] = False
        base["params_binding"] = "version_string_only"


def _recorded(bar: dict) -> dict[str, Any]:
    return {
        "side": bar.get("side"),
        "score": bar.get("score"),
        "action": bar.get("action"),
        "reason_tags": bar.get("reason_tags"),
        "factors": bar.get("factors") if isinstance(bar.get("factors"), dict) else None,
        "params_hash": bar.get("params_hash"),
    }


def _cmp(cid: str, left: float, op: str, right: float) -> dict[str, Any]:
    if op == ">=":
        met = left >= right
    elif op == "<=":
        met = left <= right
    elif op == ">":
        met = left > right
    else:
        met = left < right
    return {
        "id": cid,
        "status": "met" if met else "unmet",
        "observed": left,
        "op": op,
        "threshold": right,
        "source": "stored_score",
    }


def _side(prev: str, score: float, enter: float, exit_th: float, long_only: bool) -> str:
    if prev == "flat":
        if score >= enter:
            return "long"
        if (not long_only) and score <= -enter:
            return "short"
        return "flat"
    if prev == "long":
        if (not long_only) and score <= -enter:
            return "short"
        if score < exit_th:
            return "flat"
        return "long"
    if score >= enter:
        return "long"
    if score > -exit_th:
        return "flat"
    return "short"


def _action(prev: str, side: str) -> str:
    if side == prev:
        return "none"
    if side == "flat":
        return "exit"
    return "enter"


def _factor_status(identity: dict, bar: dict, score: float) -> dict[str, Any]:
    weights = identity.get("weights") or {}
    factors = bar.get("factors")
    if not isinstance(factors, dict) or not factors or not weights or "momentum" not in factors:
        return {"id": "factors_match_score", "status": "unknown", "source": "stored_factors"}
    parts: list[tuple[float, float]] = []
    mom = factors.get("momentum")
    if not isinstance(mom, (int, float)):
        if mom is not None:
            return {"id": "factors_match_score", "status": "unknown", "source": "stored_factors"}
        expected = 0.0
    else:
        parts.append((float(weights.get("momentum", 0.0)), float(mom)))
        imb = factors.get("volume_imbalance")
        if isinstance(imb, (int, float)) and "volume_imbalance" in weights:
            parts.append((float(weights["volume_imbalance"]), float(imb)))
        fund = factors.get("funding")
        if "funding" in weights and isinstance(fund, (int, float)):
            parts.append((float(weights["funding"]), float(fund)))
        if not parts:
            expected = 0.0
        else:
            expected = sum(weight * value for weight, value in parts) / sum(weight for weight, _ in parts)
    matched = abs(expected - score) <= 5e-5
    return {
        "id": "factors_match_score",
        "status": "met" if matched else "unmet",
        "observed": score,
        "expected": round(expected, 6),
        "source": "stored_factors",
    }


def _tag_checks(identity: dict, bar: dict) -> list[dict[str, Any]]:
    tags = bar.get("reason_tags")
    if tags is None:
        return [{"id": "reason_tags", "status": "unknown", "source": "not_stored"}]
    if not isinstance(tags, list):
        return [{"id": "reason_tags", "status": "unknown", "source": "stored_tags"}]
    recorded = {str(tag) for tag in tags}
    factors = bar.get("factors") if isinstance(bar.get("factors"), dict) else {}
    tag_abs = identity.get("tag_abs")
    checks = []
    for name in ("momentum", "volume_imbalance", "funding"):
        factor = factors.get(name)
        present = name in recorded
        if tag_abs is None or not isinstance(factor, (int, float)):
            status = "unknown"
        else:
            expect = abs(float(factor)) >= float(tag_abs)
            status = "met" if expect == present else "unmet"
        checks.append({"id": f"tag:{name}", "status": status, "recorded": present, "source": "stored_factors"})
    if "vol_regime" in recorded or "risk_gate" in recorded:
        checks.append({"id": "tag:vol_or_gate", "status": "unknown", "recorded": True, "source": "not_in_factor_snapshot"})
    return checks


def _invalidation(side: Any, score: float, exit_th: float, enter: float, long_only: bool) -> list[dict[str, Any]]:
    rows = [{"id": "stop_loss", "status": "not_in_rule"}]
    if side == "long":
        rows.append({"id": "long_score_below_exit", "status": "met" if score < exit_th else "unmet", "threshold": exit_th})
        if not long_only:
            rows.append({"id": "flip_short", "status": "met" if score <= -enter else "unmet", "threshold": -enter})
    elif side == "short":
        rows.append({"id": "short_score_above_exit", "status": "met" if score > -exit_th else "unmet", "threshold": -exit_th})
        rows.append({"id": "flip_long", "status": "met" if score >= enter else "unmet", "threshold": enter})
    else:
        rows.append({"id": "flat_entry", "status": "not_applicable"})
    return rows


def previous_bar(rows: list[dict], asof_ts: int, tf_ms: int) -> tuple[Optional[dict], bool]:
    prior = [row for row in rows if int(row["asof_ts"]) < int(asof_ts)]
    if not prior:
        return None, False
    prev = max(prior, key=lambda row: int(row["asof_ts"]))
    gap = int(tf_ms) <= 0 or int(asof_ts) - int(prev["asof_ts"]) > int(tf_ms * 1.5)
    return prev, gap
