"""HabitEngine — tag heuristics from distill-v0 (thresholds tunable, not copy-trade)."""
from __future__ import annotations

from statistics import median
from typing import Optional

from app.models.contracts import (
    HabitFeatures,
    HabitProfile,
    HabitTag,
    TraderSnapshot,
    TraderWatchlistItem,
)

# distill-v0 default heuristics
SNIPER_PCT = 0.5
SNIPER_HOLD_MAX_SEC = 180.0
MID_CURVE_PCT = 0.45
GRAD_PCT = 0.35
GRAD_POS_MEDIAN_BPS = 9000
FLIP_RATE = 0.5
FLIP_HOLD_SEC = 300.0
BAG_HOLD_SEC = 3600.0
BAG_SCORE_MIN = 0.6


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _median_int(values: list[int]) -> Optional[int]:
    if not values:
        return None
    return int(median(values))


def features_from_snapshot(snap: TraderSnapshot) -> HabitFeatures:
    entries = [int(b.progress_bps) for b in snap.recent_buys if b.progress_bps is not None]
    n = len(entries)
    if n:
        pct_lt_800 = sum(1 for p in entries if p < 800) / n
        pct_mid = sum(1 for p in entries if 800 <= p <= 7500) / n
        pct_gt_9000 = sum(1 for p in entries if p > 9000) / n
        median_entry = _median_int(entries)
    else:
        pct_lt_800 = pct_mid = pct_gt_9000 = 0.0
        median_entry = snap.entry_progress_median_bps

    hold = snap.median_hold_sec_24h
    if hold is None and snap.positions:
        hold = float(median([p.hold_sec for p in snap.positions]))

    long_pos = [p for p in snap.positions if p.hold_sec > BAG_HOLD_SEC]
    exposure = max(float(snap.gross_exposure_sol), 0.0)
    unreal_abs = sum(abs(p.unrealized_pnl_sol or 0.0) for p in snap.positions)
    bag_score = 0.0
    if hold is not None and hold > BAG_HOLD_SEC:
        bag_score += _clamp((hold - BAG_HOLD_SEC) / BAG_HOLD_SEC)
    if snap.positions:
        bag_score += 0.4 * (len(long_pos) / len(snap.positions))
    if exposure > 0:
        bag_score += 0.2 * _clamp(unreal_abs / exposure)
    bag_score = _clamp(bag_score)

    return HabitFeatures(
        median_entry_progress_bps=median_entry,
        pct_entries_lt_800=round(pct_lt_800, 4),
        pct_entries_800_7500=round(pct_mid, 4),
        pct_entries_gt_9000=round(pct_gt_9000, 4),
        median_hold_sec=hold,
        flip_rate_24h=snap.flip_rate_24h,
        bag_score=round(bag_score, 4),
    )


def _fmt(key: str, value: object) -> str:
    if isinstance(value, float):
        return f"{key}={value:.3f}"
    return f"{key}={value}"


def tag_habits(features: HabitFeatures, snap: TraderSnapshot) -> list[HabitTag]:
    tags: list[HabitTag] = []
    hold = features.median_hold_sec
    pos_bps = [int(p.progress_bps) for p in snap.positions if p.progress_bps is not None]
    pos_median = _median_int(pos_bps)

    if features.pct_entries_lt_800 >= SNIPER_PCT and hold is not None and hold <= SNIPER_HOLD_MAX_SEC:
        conf = _clamp(
            0.55
            + (features.pct_entries_lt_800 - SNIPER_PCT)
            + max(0.0, (SNIPER_HOLD_MAX_SEC - hold) / SNIPER_HOLD_MAX_SEC) * 0.3
        )
        tags.append(
            HabitTag(
                tag="sniper",
                confidence=round(conf, 4),
                evidence=[
                    _fmt("pct_entries_lt_800", features.pct_entries_lt_800),
                    _fmt("median_hold_sec", hold),
                ],
            )
        )

    if features.pct_entries_800_7500 >= MID_CURVE_PCT:
        conf = _clamp(0.5 + (features.pct_entries_800_7500 - MID_CURVE_PCT))
        tags.append(
            HabitTag(
                tag="mid_curve",
                confidence=round(conf, 4),
                evidence=[
                    _fmt("pct_entries_800_7500", features.pct_entries_800_7500),
                    _fmt("median_entry_progress_bps", features.median_entry_progress_bps),
                ],
            )
        )

    if features.pct_entries_gt_9000 >= GRAD_PCT or (
        pos_median is not None and pos_median >= GRAD_POS_MEDIAN_BPS
    ):
        conf = _clamp(
            0.5
            + max(0.0, features.pct_entries_gt_9000 - GRAD_PCT)
            + (0.2 if pos_median is not None and pos_median >= GRAD_POS_MEDIAN_BPS else 0.0)
        )
        evidence = [_fmt("pct_entries_gt_9000", features.pct_entries_gt_9000)]
        if pos_median is not None:
            evidence.append(_fmt("position_progress_median_bps", pos_median))
        tags.append(
            HabitTag(
                tag="graduation_chase",
                confidence=round(conf, 4),
                evidence=evidence,
            )
        )

    flip = features.flip_rate_24h
    if flip is not None and flip >= FLIP_RATE and hold is not None and hold < FLIP_HOLD_SEC:
        conf = _clamp(0.5 + (flip - FLIP_RATE) + max(0.0, (FLIP_HOLD_SEC - hold) / FLIP_HOLD_SEC) * 0.2)
        tags.append(
            HabitTag(
                tag="flip",
                confidence=round(conf, 4),
                evidence=[
                    _fmt("flip_rate_24h", flip),
                    _fmt("median_hold_sec", hold),
                ],
            )
        )

    if (hold is not None and hold > BAG_HOLD_SEC) or features.bag_score >= BAG_SCORE_MIN:
        conf = _clamp(0.5 + max(0.0, features.bag_score) * 0.5)
        if hold is not None and hold > BAG_HOLD_SEC:
            conf = _clamp(conf + 0.15)
        tags.append(
            HabitTag(
                tag="bag",
                confidence=round(conf, 4),
                evidence=[
                    _fmt("median_hold_sec", hold),
                    _fmt("bag_score", features.bag_score),
                    f"open_count={snap.open_count}",
                ],
            )
        )

    return tags


def apply_overrides(tags: list[HabitTag], overrides: list[str]) -> list[HabitTag]:
    have = {t.tag for t in tags}
    out = list(tags)
    for raw in overrides:
        name = str(raw).strip()
        if name not in {"sniper", "mid_curve", "graduation_chase", "flip", "bag"}:
            continue
        if name in have:
            continue
        out.append(
            HabitTag(
                tag=name,  # type: ignore[arg-type]
                confidence=1.0,
                evidence=["tags_override"],
            )
        )
    return out


def build_profile(item: TraderWatchlistItem, snap: TraderSnapshot) -> HabitProfile:
    features = features_from_snapshot(snap)
    tags = apply_overrides(tag_habits(features, snap), item.tags_override)
    tags.sort(key=lambda t: t.confidence, reverse=True)
    primary = tags[0] if tags else None
    return HabitProfile(
        watch_id=item.watch_id,
        address=item.address,
        asof_ts=snap.asof_ts,
        tags=tags,
        primary=primary,
        features=features,
    )


def habit_profile(watch_id_or_address: str, *, now_ms: Optional[int] = None) -> Optional[HabitProfile]:
    from app.traders.snapshot import get_snapshot
    from app.traders.store import get_watch

    item = get_watch(watch_id_or_address)
    if item is None:
        return None
    snap = get_snapshot(item.watch_id, now_ms=now_ms)
    if snap is None:
        return None
    return build_profile(item, snap)
