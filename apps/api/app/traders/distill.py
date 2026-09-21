"""Distill HabitTags → PumpPaperParamsPatch (own strategy, never a mirror fill).

sniper / graduation_chase must not auto-widen the entry window.
apply-distill never changes auto_paper_orders. copy_trade_enabled stays false.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Optional

from app.models.contracts import (
    DistillFeatureWeights,
    DistillResult,
    HabitProfile,
    TraderSnapshot,
)
from app.traders.habits import habit_profile
from app.traders.snapshot import get_snapshot
from app.traders.store import get_watch

COPY_TRADE_ENABLED = False
IMPACT_HARD_CAP_BPS = 80.0

DISTILL_PARAM_KEYS = {
    "progress_bps_min",
    "progress_bps_max",
    "max_impact_bps",
    "take_profit_pct",
    "stop_loss_pct",
    "max_hold_sec",
    "cooldown_sec",
    "max_day_loss_pct",
    "max_open_mints",
    "notional_pct_equity",
    "max_notional_sol",
}

FORBIDDEN_APPLY_KEYS = {
    "auto_paper_orders",
    "strategy_autopaper",
    "copy_trade_enabled",
}

SNIPER_REJECT = "sniper dominant — do not lower progress_bps_min; watchlist warning only"
GRAD_REJECT = "graduation_chase — do not raise progress_bps_max; watch_only / risk hint"


class DistillReject(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def overlay_path() -> Path:
    raw = (os.getenv("TRADER_DISTILL_OVERLAY") or "").strip()
    if raw:
        return Path(raw)
    return Path(__file__).resolve().parents[2] / "data" / "pump_paper_params_overlay.json"


def _current_params() -> dict[str, Any]:
    from app.strategies.pump_paper_v1 import get_engine

    return get_engine().params.model_dump()


def _feature_weights(snap: TraderSnapshot) -> DistillFeatureWeights:
    buy = float(snap.buy_notional_1h or 0.0)
    sell = float(snap.sell_notional_1h or 0.0)
    momentum = 1.0
    if buy > 0 and buy >= 2.0 * max(sell, 1e-9):
        momentum = 1.25
    impact = 1.0
    sizes = [t.sol_amount for t in (snap.recent_buys + snap.recent_sells)]
    if sizes and max(sizes) >= 0.2:
        impact = 1.2  # more averse; paper still capped at max_impact_bps=80
    return DistillFeatureWeights(progress=1.0, momentum=momentum, impact=impact)


def _paper_compare(profile: HabitProfile) -> dict[str, Any]:
    return {
        "self_stats_ref": "GET /api/v1/strategy/pump-paper-v1/stats",
        "trader_ref_curve_id": f"ref:{profile.address}",
        "note": "reference_only — not copy-trading",
    }


def distill_profile(
    profile: HabitProfile,
    snap: TraderSnapshot,
    *,
    current: Optional[dict[str, Any]] = None,
) -> DistillResult:
    params = current if current is not None else _current_params()
    primary = profile.primary
    tags = [t.tag for t in profile.tags]
    suggested: dict[str, Any] = {}
    reject_reason: Optional[str] = None
    feat = profile.features

    if primary is None:
        reject_reason = "no_primary_habit"
    elif primary.tag == "sniper":
        reject_reason = SNIPER_REJECT
        # Do not lower progress_bps_min.
    elif primary.tag == "graduation_chase":
        reject_reason = GRAD_REJECT
        # Do not raise progress_bps_max.
    elif primary.tag == "mid_curve":
        median_bps = feat.median_entry_progress_bps
        if median_bps is not None:
            half = 1500
            cur_min = int(params.get("progress_bps_min", 800))
            cur_max = int(params.get("progress_bps_max", 7500))
            new_min = max(cur_min, int(median_bps) - half)
            new_max = min(cur_max, int(median_bps) + half)
            if new_min > new_max:
                new_min, new_max = cur_min, cur_max
            suggested["progress_bps_min"] = int(new_min)
            suggested["progress_bps_max"] = int(new_max)
    elif primary.tag == "flip":
        cur_hold = int(params.get("max_hold_sec", 420))
        suggested["max_hold_sec"] = int(min(cur_hold, 180))
        cur_tp = float(params.get("take_profit_pct", 0.14))
        suggested["take_profit_pct"] = round(max(0.08, cur_tp * 0.8), 4)
    elif primary.tag == "bag":
        cur_hold = int(params.get("max_hold_sec", 420))
        suggested["max_hold_sec"] = int(max(cur_hold, 3600))
        cur_sl = float(params.get("stop_loss_pct", 0.09))
        suggested["stop_loss_pct"] = round(max(0.04, min(cur_sl, 0.08)), 4)

    # Never propose raising the impact hard cap; paper remains gated at 80.
    if "max_impact_bps" in suggested and float(suggested["max_impact_bps"]) > IMPACT_HARD_CAP_BPS:
        suggested["max_impact_bps"] = IMPACT_HARD_CAP_BPS

    for banned in FORBIDDEN_APPLY_KEYS:
        suggested.pop(banned, None)

    return DistillResult(
        source_watch_id=profile.watch_id,
        asof_ts=profile.asof_ts,
        suggested_params=suggested,
        feature_weights=_feature_weights(snap),
        enabled_tags=tags,
        reject_reason=reject_reason,
        paper_compare=_paper_compare(profile),
    )


def distill_watch(watch_id_or_address: str, *, now_ms: Optional[int] = None) -> Optional[DistillResult]:
    item = get_watch(watch_id_or_address)
    if item is None:
        return None
    profile = habit_profile(item.watch_id, now_ms=now_ms)
    snap = get_snapshot(item.watch_id, now_ms=now_ms)
    if profile is None or snap is None:
        return None
    return distill_profile(profile, snap)


def sanitize_patch(
    patch: dict[str, Any],
    current: dict[str, Any],
) -> dict[str, Any]:
    """Drop forbidden keys; refuse sniper/grad window widening."""
    clean: dict[str, Any] = {}
    for key, value in patch.items():
        if key in FORBIDDEN_APPLY_KEYS:
            continue
        if key not in DISTILL_PARAM_KEYS:
            continue
        clean[key] = value

    cur_min = int(current.get("progress_bps_min", 800))
    cur_max = int(current.get("progress_bps_max", 7500))
    if "progress_bps_min" in clean and int(clean["progress_bps_min"]) < cur_min:
        raise DistillReject(
            "DISTILL_WINDOW_GUARD",
            "refusing to lower progress_bps_min (sniper must not auto-widen entry window)",
        )
    if "progress_bps_max" in clean and int(clean["progress_bps_max"]) > cur_max:
        raise DistillReject(
            "DISTILL_WINDOW_GUARD",
            "refusing to raise progress_bps_max (graduation_chase must not auto-widen entry window)",
        )
    if "max_impact_bps" in clean:
        clean["max_impact_bps"] = min(float(clean["max_impact_bps"]), IMPACT_HARD_CAP_BPS)
    if "max_day_loss_pct" in clean and float(clean["max_day_loss_pct"]) > float(
        current.get("max_day_loss_pct", 0.05)
    ):
        clean.pop("max_day_loss_pct", None)
    return clean


def persist_overlay(payload: dict[str, Any]) -> Path:
    path = overlay_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def apply_distill(
    *,
    confirm: bool,
    source_watch_id: str,
    suggested_params: Optional[dict[str, Any]] = None,
    now_ms: Optional[int] = None,
) -> dict[str, Any]:
    """Write params only when confirm=true. Never toggles auto_paper_orders."""
    if not confirm:
        raise DistillReject("CONFIRM_REQUIRED", "apply-distill requires confirm=true")

    result = distill_watch(source_watch_id, now_ms=now_ms)
    if result is None:
        raise DistillReject("NOT_FOUND", f"watch not found: {source_watch_id}")
    if result.reject_reason:
        raise DistillReject("DISTILL_REJECTED", result.reject_reason)

    from app.strategies.pump_paper_v1 import get_engine

    engine = get_engine()
    prev_auto = bool(engine.params.auto_paper_orders)
    current = engine.params.model_dump()
    incoming = suggested_params if suggested_params is not None else result.suggested_params
    if not incoming:
        raise DistillReject("EMPTY_PATCH", "no suggested_params to apply")
    patch = sanitize_patch(dict(incoming), current)
    if not patch:
        raise DistillReject("EMPTY_PATCH", "no allowed PumpPaperParams keys to apply")

    engine.update_params(patch)
    if bool(engine.params.auto_paper_orders) != prev_auto:
        engine.update_params({"auto_paper_orders": prev_auto})

    overlay = {
        "copy_trade_enabled": False,
        "auto_paper_orders_untouched": True,
        "source_watch_id": result.source_watch_id,
        "applied_ts": int(now_ms if now_ms is not None else time.time() * 1000),
        "params": patch,
        "enabled_tags": result.enabled_tags,
        "liveDisabled": True,
    }
    path = persist_overlay(overlay)
    return {
        "applied": patch,
        "source_watch_id": result.source_watch_id,
        "enabled_tags": result.enabled_tags,
        "auto_paper_orders": bool(engine.params.auto_paper_orders),
        "copy_trade_enabled": False,
        "liveDisabled": True,
        "overlay_path": str(path),
        "params": engine.params.model_dump(),
        "confirm": True,
    }
