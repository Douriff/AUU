"""Paper-only shadow parameter comparison (HabitTag-driven).

Virtual enter/skip/exit on the same tape the frozen main window sees.
Does not submit paper orders, does not write PaperTradeJournal, DecisionLog,
executability inputs, or HabitProfile. liveEnabled stays false.

Shadow sets may change momentum thresholds, take-profit, stop-loss, and hold.
progress_* is rejected (SHADOW_PROGRESS_FORBIDDEN). Default: comparison off.
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from app.data_paths import data_dir, guarded_path
from app.models.contracts import HabitProfile, PumpfunPaperSnapshot, TraderSnapshot
from app.legacy.pump.paper.postmortem import by_exit_reason
from app.legacy.pump.providers.pumpfun_curve_math import (
    DEFAULT_PROTOCOL_FEE_BPS,
    LAMPORTS_PER_SOL,
    buy_tokens_out,
    sell_sol_out,
    split_impact_gross_fee_net,
)
from app.legacy.pump.strategies.pump_paper_v1 import (
    STRATEGY_ID,
    PositionState,
    PumpPaperParams,
    TapeWindow,
    _entry_ttl_ms,
    _observed_fee_meta,
    curve_impact_bps,
    evaluate,
    snapshot_from_trade,
)

MAX_SHADOW_SETS = 3
SAMPLE_MIN_N = 30
GO_WINDOW_LABEL = "round8b"
NOTE_NEVER_LIVE = "shadow results never enable live"
JOURNAL_CAP = 2_000

ALLOWED_KEYS = (
    "min_trade_count_1m",
    "min_buy_sell_ratio_1m",
    "take_profit_pct",
    "stop_loss_pct",
    "max_hold_sec",
)

_REASON_TAG = {
    "take_profit": "TAKE_PROFIT",
    "stop_loss": "STOP_LOSS",
    "max_hold": "MAX_HOLD",
    "sell_pressure": "SELL_PRESSURE",
    "graduation": "CURVE_NEAR_GRADUATION",
    "impact_split": "SPLIT_REDUCE",
}

_LOCK = threading.Lock()


class ShadowProgressForbidden(Exception):
    """Configured shadow set touched progress_* — HTTP 400."""

    code = "SHADOW_PROGRESS_FORBIDDEN"

    def __init__(self, touched: list[str]):
        self.touched = list(touched)
        super().__init__(f"shadow sets forbid progress fields: {', '.join(self.touched)}")


class ShadowConfigError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


@dataclass
class ShadowSetSpec:
    id: str
    label: str = ""
    habit_tag: Optional[str] = None
    watch_id: Optional[str] = None
    source: str = "config"  # config | habit
    setup_seed_tags: list[str] = field(default_factory=list)
    min_trade_count_1m: Optional[int] = None
    min_buy_sell_ratio_1m: Optional[float] = None
    take_profit_pct: Optional[float] = None
    stop_loss_pct: Optional[float] = None
    max_hold_sec: Optional[int] = None

    def delta(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key in ALLOWED_KEYS:
            value = getattr(self, key)
            if value is not None:
                out[key] = value
        return out

    def as_public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label or self.id,
            "habit_tag": self.habit_tag,
            "watch_id": self.watch_id,
            "source": self.source,
            "setup_seed_tags": list(self.setup_seed_tags),
            "params": self.delta(),
        }


@dataclass
class _Config:
    enabled: bool = False
    sets: list[ShadowSetSpec] = field(default_factory=list)


@dataclass
class _Open:
    symbol: str
    mint: str
    entry_price: float
    entry_ts: int
    entry_notional: float
    tokens: int
    sol_lamports: int
    entry_impact_net_bps: Optional[float] = None


_config = _Config()
_opens: dict[tuple[str, str], _Open] = {}
_last_open_ms: dict[tuple[str, str], int] = {}
_skip_key: dict[tuple[str, str], tuple] = {}
# Real-market virtual orders waiting for the first real print after latency.
# In memory only (a restart drops them, like the main engine's deferrals).
_pending: dict[tuple[str, str], dict[str, Any]] = {}
_closed: list[dict[str, Any]] = []
_decisions: list[dict[str, Any]] = []
_loaded = False
_PERSIST_LOCK = threading.Lock()


def _store_path() -> Path:
    raw = (os.getenv("SHADOW_COMPARE_STORE") or "").strip()
    if raw:
        path = Path(raw).expanduser()
    else:
        path = data_dir() / "shadow_compare.json"
    return guarded_path(path)


def _counters(closed: list[dict[str, Any]], *, enabled: bool, n_sets: int) -> dict[str, Any]:
    by_set: dict[str, dict[str, Any]] = {}
    wins = 0
    pnl_sum = 0.0
    for row in closed:
        sid = str(row.get("set_id") or "")
        try:
            pnl = float(row.get("pnl") or 0.0)
        except (TypeError, ValueError):
            pnl = 0.0
        bucket = by_set.setdefault(sid, {"n": 0, "wins": 0, "pnl_sum": 0.0})
        bucket["n"] += 1
        bucket["pnl_sum"] += pnl
        pnl_sum += pnl
        if pnl > 0:
            wins += 1
            bucket["wins"] += 1
    return {
        "enabled": bool(enabled),
        "n_sets": int(n_sets),
        "n_closed": len(closed),
        "wins": wins,
        "pnl_sum": pnl_sum,
        "by_set": by_set,
    }


def _delete_store() -> None:
    path = _store_path()
    for candidate in (path, path.with_suffix(path.suffix + ".tmp")):
        guarded_path(candidate)
        try:
            candidate.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


def _closed_for_disk(row: Mapping[str, Any]) -> dict[str, Any]:
    item = dict(row)
    if item.get("market_source") == "legacy_synthetic":
        item.pop("market_source", None)
    return item


def _persist_shadow() -> None:
    """Write config + closed rows (counters are derived from those rows)."""
    with _LOCK:
        payload = {
            "v": 1,
            "enabled": bool(_config.enabled),
            "sets": [spec.as_public() for spec in _config.sets],
            "closed": [_closed_for_disk(row) for row in _closed],
            "counters": _counters(_closed, enabled=_config.enabled, n_sets=len(_config.sets)),
        }
    path = guarded_path(_store_path())
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        text = json.dumps(payload)
        with _PERSIST_LOCK:
            tmp.write_text(text, encoding="utf-8")
            tmp.replace(path)
    except OSError:
        return


def _restore_locked(data: Mapping[str, Any]) -> None:
    global _config
    specs: list[ShadowSetSpec] = []
    raw_sets = data.get("sets") or []
    if isinstance(raw_sets, list):
        for i, raw in enumerate(raw_sets):
            if not isinstance(raw, Mapping):
                continue
            flat = dict(raw)
            params = flat.pop("params", None)
            if isinstance(params, Mapping):
                for key, value in params.items():
                    flat.setdefault(key, value)
            try:
                specs.append(_spec_from_mapping(flat, i))
            except (ShadowConfigError, ShadowProgressForbidden):
                continue
    closed: list[dict[str, Any]] = []
    raw_closed = data.get("closed") or []
    if isinstance(raw_closed, list):
        for row in raw_closed:
            if isinstance(row, dict) and row.get("set_id"):
                item = dict(row)
                if "market_source" not in item:
                    item["market_source"] = "legacy_synthetic"
                closed.append(item)
    if len(closed) > JOURNAL_CAP:
        closed = closed[-JOURNAL_CAP:]
    _config = _Config(enabled=bool(data.get("enabled")), sets=specs)
    _closed[:] = closed


def _ensure_loaded() -> None:
    global _loaded
    with _LOCK:
        if _loaded:
            return
        path = _store_path()
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            _loaded = True
            return
        except OSError:
            _loaded = True
            return
        try:
            raw = json.loads(text)
        except json.JSONDecodeError:
            _loaded = True
            return
        if isinstance(raw, dict):
            _restore_locked(raw)
        _loaded = True


def reset_shadow_compare() -> None:
    """Drop config, virtual positions, and the shadow journal. Paper journal untouched.

    Also deletes the on-disk store so a later load cannot resurrect test state.
    """
    global _config, _loaded
    with _LOCK:
        _config = _Config()
        _opens.clear()
        _pending.clear()
        _last_open_ms.clear()
        _skip_key.clear()
        _closed.clear()
        _decisions.clear()
        _loaded = True
    _delete_store()


def reload_shadow_from_disk() -> None:
    """Drop memory and read the store again. Used to simulate an API restart."""
    global _config, _loaded
    with _LOCK:
        _config = _Config()
        _opens.clear()
        _pending.clear()
        _last_open_ms.clear()
        _skip_key.clear()
        _closed.clear()
        _decisions.clear()
        _loaded = False
    _ensure_loaded()


def shadow_enabled() -> bool:
    _ensure_loaded()
    with _LOCK:
        return bool(_config.enabled)


def progress_keys(payload: Any) -> list[str]:
    found: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, Mapping):
            for key, value in node.items():
                if str(key).lower().startswith("progress"):
                    found.append(str(key))
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(payload)
    return found


def _require_no_progress(payload: Any) -> None:
    touched = progress_keys(payload)
    if touched:
        raise ShadowProgressForbidden(touched)


def _as_float(value: Any, key: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as e:
        raise ShadowConfigError("SHADOW_PARAM_INVALID", f"{key} must be a number") from e


def _as_int(value: Any, key: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as e:
        raise ShadowConfigError("SHADOW_PARAM_INVALID", f"{key} must be an integer") from e


def _check_delta(delta: Mapping[str, Any]) -> dict[str, Any]:
    clean: dict[str, Any] = {}
    if "min_trade_count_1m" in delta and delta["min_trade_count_1m"] is not None:
        n = _as_int(delta["min_trade_count_1m"], "min_trade_count_1m")
        if n < 0 or n > 10_000:
            raise ShadowConfigError("SHADOW_PARAM_INVALID", "min_trade_count_1m out of range")
        clean["min_trade_count_1m"] = n
    if "min_buy_sell_ratio_1m" in delta and delta["min_buy_sell_ratio_1m"] is not None:
        r = _as_float(delta["min_buy_sell_ratio_1m"], "min_buy_sell_ratio_1m")
        if r < 0 or r > 100:
            raise ShadowConfigError("SHADOW_PARAM_INVALID", "min_buy_sell_ratio_1m out of range")
        clean["min_buy_sell_ratio_1m"] = r
    if "take_profit_pct" in delta and delta["take_profit_pct"] is not None:
        tp = _as_float(delta["take_profit_pct"], "take_profit_pct")
        if tp <= 0 or tp > 1:
            raise ShadowConfigError("SHADOW_PARAM_INVALID", "take_profit_pct out of range")
        clean["take_profit_pct"] = tp
    if "stop_loss_pct" in delta and delta["stop_loss_pct"] is not None:
        sl = _as_float(delta["stop_loss_pct"], "stop_loss_pct")
        if sl <= 0 or sl > 0.5:
            raise ShadowConfigError("SHADOW_PARAM_INVALID", "stop_loss_pct out of range")
        clean["stop_loss_pct"] = sl
    if "max_hold_sec" in delta and delta["max_hold_sec"] is not None:
        hold = _as_int(delta["max_hold_sec"], "max_hold_sec")
        if hold < 1 or hold > 86_400:
            raise ShadowConfigError("SHADOW_PARAM_INVALID", "max_hold_sec out of range")
        clean["max_hold_sec"] = hold
    return clean


def _seed_tags(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        parts = [p.strip() for p in raw.split(",")]
    elif isinstance(raw, list):
        parts = [str(p).strip() for p in raw]
    else:
        raise ShadowConfigError("SHADOW_PARAM_INVALID", "setup_seed_tags must be a list of labels")
    out: list[str] = []
    for part in parts:
        if not part or part in out:
            continue
        out.append(part[:64])
        if len(out) >= 8:
            break
    return out


def _spec_from_mapping(raw: Mapping[str, Any], index: int) -> ShadowSetSpec:
    _require_no_progress(raw)
    delta = _check_delta(raw)
    habit = raw.get("habit_tag")
    habit_s = str(habit).strip() if habit else None
    label = str(raw.get("label") or habit_s or f"shadow-{index + 1}").strip()
    ident = str(raw.get("id") or habit_s or label or f"shadow-{index + 1}").strip()
    ident = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in ident)[:40] or f"shadow-{index + 1}"
    watch = raw.get("watch_id")
    source = str(raw.get("source") or "config").strip().lower()
    if source not in {"config", "habit"}:
        source = "config"
    return ShadowSetSpec(
        id=ident,
        label=label[:48],
        habit_tag=habit_s,
        watch_id=str(watch).strip() if watch else None,
        source=source,
        setup_seed_tags=_seed_tags(raw.get("setup_seed_tags")),
        min_trade_count_1m=delta.get("min_trade_count_1m"),
        min_buy_sell_ratio_1m=delta.get("min_buy_sell_ratio_1m"),
        take_profit_pct=delta.get("take_profit_pct"),
        stop_loss_pct=delta.get("stop_loss_pct"),
        max_hold_sec=delta.get("max_hold_sec"),
    )


def _same_number(current: Any, proposed: Any) -> bool:
    try:
        return abs(float(current) - float(proposed)) < 1e-9
    except (TypeError, ValueError):
        return current == proposed


def delta_from_habit(
    profile: HabitProfile,
    snap: TraderSnapshot,
    main: PumpPaperParams,
) -> dict[str, Any]:
    """Habit factors → momentum / TP / SL / hold only.

    Reuses distill suggestions, then drops every progress_* key. Seed labels
    are not read from the profile and this function does not write HabitProfile.
    """
    from app.legacy.pump.traders.distill import distill_profile

    distilled = distill_profile(profile, snap, current=main.model_dump())
    merged: dict[str, Any] = {}
    for key, value in distilled.suggested_params.items():
        if key in ALLOWED_KEYS:
            merged[key] = value
    merged.update(_habit_timing_delta(profile, main))
    # Distill wins on keys it actually suggested (flip hold/TP, bag hold/SL).
    for key, value in distilled.suggested_params.items():
        if key in ALLOWED_KEYS:
            merged[key] = value
    clean: dict[str, Any] = {}
    for key in ALLOWED_KEYS:
        if key not in merged or merged[key] is None:
            continue
        if _same_number(getattr(main, key), merged[key]):
            continue
        clean[key] = merged[key]
    return clean


def _habit_timing_delta(profile: HabitProfile, main: PumpPaperParams) -> dict[str, Any]:
    """Momentum and hold from observed habit features. Never progress_*."""
    primary = profile.primary
    if primary is None:
        return {}
    tag = primary.tag
    feat = profile.features
    hold = feat.median_hold_sec
    out: dict[str, Any] = {}
    if tag == "sniper":
        out["min_trade_count_1m"] = max(4, int(main.min_trade_count_1m) - 4)
        out["min_buy_sell_ratio_1m"] = round(max(1.5, float(main.min_buy_sell_ratio_1m) * 0.85), 4)
        if hold is not None:
            out["max_hold_sec"] = max(30, min(int(main.max_hold_sec), int(hold)))
    elif tag == "graduation_chase":
        out["min_trade_count_1m"] = int(main.min_trade_count_1m) + 5
        out["max_hold_sec"] = max(30, int(int(main.max_hold_sec) * 0.5))
        out["stop_loss_pct"] = round(max(0.04, float(main.stop_loss_pct) * 0.9), 4)
    elif tag == "mid_curve":
        out["min_buy_sell_ratio_1m"] = round(float(main.min_buy_sell_ratio_1m) * 1.1, 4)
        if hold is not None:
            out["max_hold_sec"] = max(30, min(600, int(hold)))
    elif tag == "flip":
        if feat.flip_rate_24h is not None and float(feat.flip_rate_24h) >= 0.5:
            out["min_trade_count_1m"] = max(4, int(main.min_trade_count_1m) - 2)
    elif tag == "bag" and hold is not None and int(hold) > int(main.max_hold_sec):
        out["max_hold_sec"] = min(3600, int(hold))
    return out


def derive_sets_from_habits(
    main: PumpPaperParams,
    *,
    limit: int = MAX_SHADOW_SETS,
    setup_seed_tags: Optional[Sequence[str]] = None,
) -> list[ShadowSetSpec]:
    """Read watchlist HabitTags. Does not write HabitProfile or the main params."""
    from app.legacy.pump.traders.habits import habit_profile
    from app.legacy.pump.traders.snapshot import get_snapshot
    from app.legacy.pump.traders.store import list_watches

    seeds = _seed_tags(list(setup_seed_tags or []))
    specs: list[ShadowSetSpec] = []
    seen: set[str] = set()
    for item in list_watches():
        if len(specs) >= limit:
            break
        if not item.enabled:
            continue
        profile = habit_profile(item.watch_id)
        snap = get_snapshot(item.watch_id)
        if profile is None or snap is None or profile.primary is None:
            continue
        tag = profile.primary.tag
        if tag in seen:
            continue
        delta = delta_from_habit(profile, snap, main)
        if not delta:
            continue
        seen.add(tag)
        specs.append(
            ShadowSetSpec(
                id=f"habit-{tag}",
                label=tag,
                habit_tag=tag,
                watch_id=profile.watch_id,
                source="habit",
                setup_seed_tags=list(seeds),
                min_trade_count_1m=delta.get("min_trade_count_1m"),
                min_buy_sell_ratio_1m=delta.get("min_buy_sell_ratio_1m"),
                take_profit_pct=delta.get("take_profit_pct"),
                stop_loss_pct=delta.get("stop_loss_pct"),
                max_hold_sec=delta.get("max_hold_sec"),
            )
        )
    return specs


def _sets_changed(prev: list[ShadowSetSpec], nxt: list[ShadowSetSpec]) -> bool:
    def key(spec: ShadowSetSpec) -> tuple:
        return (
            spec.id,
            spec.habit_tag,
            tuple(spec.setup_seed_tags),
            tuple(sorted((k, spec.delta()[k]) for k in spec.delta())),
        )

    return [key(s) for s in prev] != [key(s) for s in nxt]


def apply_shadow_config(body: Mapping[str, Any], *, main: Optional[PumpPaperParams] = None) -> dict[str, Any]:
    """Replace shadow config. Never writes strategy params or HabitProfile."""
    _ensure_loaded()
    if not isinstance(body, Mapping):
        raise ShadowConfigError("SHADOW_PARAM_INVALID", "body must be an object")
    _require_no_progress(body)
    main_params = main if main is not None else _main_params()
    derive = bool(body.get("derive_from_habits") or body.get("from_habits"))
    seeds = _seed_tags(body.get("setup_seed_tags")) if "setup_seed_tags" in body else None

    with _LOCK:
        enabled = _config.enabled if "enabled" not in body else bool(body.get("enabled"))
        current_sets = list(_config.sets)

    if "sets" in body and body.get("sets") is not None:
        raw_sets = body.get("sets")
        if not isinstance(raw_sets, list):
            raise ShadowConfigError("SHADOW_PARAM_INVALID", "sets must be a list")
        specs = [_spec_from_mapping(row, i) for i, row in enumerate(raw_sets) if isinstance(row, Mapping)]
        if len(specs) != len(raw_sets):
            raise ShadowConfigError("SHADOW_PARAM_INVALID", "each shadow set must be an object")
    else:
        specs = list(current_sets)

    if derive:
        derived = derive_sets_from_habits(
            main_params,
            limit=MAX_SHADOW_SETS,
            setup_seed_tags=seeds if seeds is not None else [],
        )
        if "sets" in body and body.get("sets"):
            have = {s.habit_tag for s in specs if s.habit_tag}
            for spec in derived:
                if len(specs) >= MAX_SHADOW_SETS:
                    break
                if spec.habit_tag in have or any(s.id == spec.id for s in specs):
                    continue
                specs.append(spec)
        else:
            specs = derived

    if seeds is not None and not derive:
        for spec in specs:
            if not spec.setup_seed_tags:
                spec.setup_seed_tags = list(seeds)

    if len(specs) > MAX_SHADOW_SETS:
        raise ShadowConfigError(
            "SHADOW_SET_LIMIT",
            f"at most {MAX_SHADOW_SETS} shadow sets",
        )
    ids = [s.id for s in specs]
    if len(ids) != len(set(ids)):
        raise ShadowConfigError("SHADOW_SET_DUPLICATE", "shadow set ids must be unique")

    with _LOCK:
        changed = _sets_changed(_config.sets, specs)
        _config.enabled = enabled
        _config.sets = specs
        if changed:
            _opens.clear()
            _pending.clear()
            _last_open_ms.clear()
            _skip_key.clear()
            _closed.clear()
            _decisions.clear()
    _persist_shadow()
    return build_shadow_compare()


def _main_params() -> PumpPaperParams:
    from app.legacy.pump.strategies.pump_paper_v1 import get_engine

    return get_engine().params


def shadow_params(main: PumpPaperParams, spec: ShadowSetSpec) -> PumpPaperParams:
    """Copy of the frozen window with only allowed overrides. progress_* stays."""
    return main.model_copy(update=spec.delta())


def _reserves(snap: PumpfunPaperSnapshot) -> tuple[int, int, int, int, int]:
    return (
        int(float(snap.virtual_sol_reserves)),
        int(float(snap.virtual_token_reserves)),
        int(float(snap.real_sol_reserves)),
        int(float(snap.real_token_reserves)),
        int(snap.creator_fee_bps or 0),
    )


def _fees_for(snap: PumpfunPaperSnapshot, fees: Optional[tuple[int, int]]) -> tuple[int, int]:
    if fees is not None:
        return int(fees[0]), int(fees[1])
    return DEFAULT_PROTOCOL_FEE_BPS, int(snap.creator_fee_bps or 0)


def _market_source(snap: PumpfunPaperSnapshot) -> str:
    """Label for a shadow row: synthetic snapshot, else the active provider kind."""
    if snap.synthetic:
        return "synthetic"
    from app.providers import market_data_kind

    return market_data_kind()


def _defers(snap: PumpfunPaperSnapshot) -> bool:
    """Real-market tape: virtual orders wait for the first real print after latency."""
    if snap.synthetic:
        return False
    from app.providers import market_data_kind

    return market_data_kind() == "real"


def _real_fees(snap: PumpfunPaperSnapshot) -> Optional[tuple[int, int]]:
    """Same fee resolution as main real-market paper fills; None off the real tape."""
    if not _defers(snap):
        return None
    from app.legacy.pump.paper.real_fill import curve_fee_bps

    return curve_fee_bps(_observed_fee_meta(snap.symbol), snap)


def quote_entry(
    snap: PumpfunPaperSnapshot,
    notional_sol: float,
    fees: Optional[tuple[int, int]] = None,
) -> Optional[tuple[float, int, int]]:
    """Curve buy quote: (entry price in price_sol units, tokens, sol lamports in).

    Fees match buy_tokens_out (protocol + creator). None when the curve cannot fill.
    """
    vs, vt, _rs, rt, _creator = _reserves(snap)
    proto, creator = _fees_for(snap, fees)
    sol_lamports = int(abs(float(notional_sol)) * LAMPORTS_PER_SOL)
    if sol_lamports <= 0:
        return None
    tokens = buy_tokens_out(vs, vt, rt, sol_lamports, proto, creator)
    if tokens <= 0:
        return None
    return sol_lamports / tokens, int(tokens), sol_lamports


def quote_exit_sol(
    snap: PumpfunPaperSnapshot, tokens: int, fees: Optional[tuple[int, int]] = None
) -> float:
    """Net SOL back to the seller after protocol + creator fee."""
    vs, vt, _rs, _rt, _creator = _reserves(snap)
    proto, creator = _fees_for(snap, fees)
    net, _gross = sell_sol_out(vs, vt, int(tokens), proto, creator)
    return net / LAMPORTS_PER_SOL


def _latency_ms() -> int:
    from app.paper.broker import get_paper_broker

    return int(get_paper_broker().latency_ms)


def _drain_pending(spec: ShadowSetSpec, symbol: str, now_ms: int) -> bool:
    """Fill a deferred real-market virtual order on the first print after latency.

    Same rule as the main engine: the order reaches the curve
    ``broker.latency_ms`` after the decision and executes on the reserves of
    the first real trade received at or after that moment. A quiet tape fills
    nothing; entries expire after ``LIVE_PAPER_ENTRY_TTL_MS``, exits wait.
    Returns True while an order is still pending (the set skips evaluation).
    """
    key = (spec.id, symbol)
    with _LOCK:
        pending = _pending.get(key)
    if pending is None:
        return False
    from app.providers import get_provider

    ready = int(pending["ts"]) + _latency_ms()
    finder = getattr(get_provider(), "first_trade_after", None)
    trade = finder(symbol, ready) if callable(finder) else None
    if trade and trade.get("mint") and pending["mint"] and trade["mint"] != pending["mint"]:
        # Symbol re-used by another mint after eviction: never fill across tokens.
        with _LOCK:
            _pending.pop(key, None)
        return False
    snap = snapshot_from_trade(symbol, trade, pending["mint"]) if trade else None
    if snap is None:
        if pending["action"] == "enter" and now_ms - ready > _entry_ttl_ms():
            with _LOCK:
                _pending.pop(key, None)
                _record_decision(
                    {
                        "ts": now_ms,
                        "set_id": spec.id,
                        "symbol": symbol,
                        "mint": pending["mint"],
                        "action": "skip",
                        "reason": "no_real_print",
                        "habit_tag": spec.habit_tag,
                        "setup_seed_tags": list(spec.setup_seed_tags),
                    },
                    debounce=True,
                )
            return False
        return True
    with _LOCK:
        _pending.pop(key, None)
        pos = _opens.get(key)
    fill_ts = int(trade.get("ts") or now_ms)
    if pending["action"] == "enter":
        _open_virtual(
            spec, snap, fill_ts, pending["notional"], pending["impact_entry_bps"], pending["reason"]
        )
    elif pos is not None:
        _close_virtual(spec, pos, snap, fill_ts, pending["reason"])
    return False


def _defer(spec: ShadowSetSpec, snapshot: PumpfunPaperSnapshot, action: str, now_ms: int, **extra: Any) -> None:
    with _LOCK:
        _pending.setdefault(
            (spec.id, snapshot.symbol),
            {"action": action, "ts": int(now_ms), "mint": snapshot.mint, **extra},
        )


def hypothetical_net(entry_sol: float, exit_sol: float) -> tuple[float, float]:
    pnl = float(exit_sol) - float(entry_sol)
    base = abs(float(entry_sol))
    net_bps = (pnl / base) * 1e4 if base > 0 else 0.0
    return pnl, net_bps


def _entry_net_bps(snap: PumpfunPaperSnapshot, notional: float, impact_entry_bps: float) -> Optional[float]:
    gross = None if impact_entry_bps >= 1e8 else float(impact_entry_bps)
    if gross is None:
        try:
            gross = float(curve_impact_bps(snap, notional, "buy"))
        except (ValueError, TypeError, ZeroDivisionError):
            return None
    _g, _fee, net = split_impact_gross_fee_net(gross, phase=snap.phase or "curve")
    return net


def _record_decision(row: dict[str, Any], *, debounce: bool) -> None:
    key = (row["set_id"], row["symbol"])
    marker = (row["action"], row["reason"])
    if debounce and _skip_key.get(key) == marker:
        return
    _skip_key[key] = marker
    _decisions.append(row)
    if len(_decisions) > JOURNAL_CAP:
        del _decisions[: len(_decisions) - JOURNAL_CAP + 200]
    if row.get("action") != "exit":
        try:
            from app.paper.events import note_shadow_decision

            note_shadow_decision(row)
        except Exception:
            pass


def observe_candidate(
    *,
    symbol: str,
    snapshot: PumpfunPaperSnapshot,
    tape: TapeWindow,
    now_ms: int,
    notional_sol: float,
    impact_entry_bps: float,
    sell_pressure_ms: int = 0,
    main_params: Optional[PumpPaperParams] = None,
) -> None:
    """Virtual decision for each enabled shadow set. No paper order, no main stats."""
    _ensure_loaded()
    with _LOCK:
        if not _config.enabled or not _config.sets:
            return
        specs = list(_config.sets)
    params = main_params if main_params is not None else _main_params()
    notional = float(notional_sol)
    for spec in specs:
        _observe_one(
            spec,
            params=params,
            symbol=symbol,
            snapshot=snapshot,
            tape=tape,
            now_ms=now_ms,
            notional=notional,
            impact_entry_bps=float(impact_entry_bps),
            sell_pressure_ms=int(sell_pressure_ms),
        )


def _observe_one(
    spec: ShadowSetSpec,
    *,
    params: PumpPaperParams,
    symbol: str,
    snapshot: PumpfunPaperSnapshot,
    tape: TapeWindow,
    now_ms: int,
    notional: float,
    impact_entry_bps: float,
    sell_pressure_ms: int,
) -> None:
    shadow = shadow_params(params, spec)
    if _defers(snapshot) and _drain_pending(spec, symbol, now_ms):
        return
    with _LOCK:
        pos = _opens.get((spec.id, symbol))
        open_count = sum(1 for (sid, _sym) in _opens if sid == spec.id)
        last_open = _last_open_ms.get((spec.id, snapshot.mint))
    position = None
    impact_out = 0.0
    if pos is not None:
        position = PositionState(
            mint=pos.mint,
            symbol=pos.symbol,
            qty=1.0,
            entry_price=pos.entry_price,
            entry_ts=pos.entry_ts,
            entry_notional=pos.entry_notional,
        )
        try:
            impact_out = curve_impact_bps(snapshot, max(pos.entry_notional, 1e-9), "sell")
        except (ValueError, TypeError, ZeroDivisionError):
            impact_out = 0.0
    signal = evaluate(
        snapshot=snapshot,
        tape=tape,
        params=shadow,
        now_ms=now_ms,
        impact_entry_bps=impact_entry_bps,
        impact_exit_bps=impact_out,
        position=position,
        last_open_ts=last_open,
        open_mint_count=open_count,
        sell_pressure_ms=sell_pressure_ms,
        reject_cooldown=False,
    )
    if pos is None:
        if signal.side == "long" and signal.reason == "pump_paper_v1_entry":
            if not _defers(snapshot):
                _open_virtual(spec, snapshot, now_ms, notional, impact_entry_bps, signal.reason)
            else:
                _defer(
                    spec,
                    snapshot,
                    "enter",
                    now_ms,
                    notional=float(notional),
                    impact_entry_bps=float(impact_entry_bps),
                    reason=signal.reason,
                )
        else:
            with _LOCK:
                _record_decision(
                    {
                        "ts": now_ms,
                        "set_id": spec.id,
                        "symbol": symbol,
                        "mint": snapshot.mint,
                        "action": "skip",
                        "reason": signal.reason or "skip",
                        "habit_tag": spec.habit_tag,
                        "setup_seed_tags": list(spec.setup_seed_tags),
                    },
                    debounce=True,
                )
        return
    if signal.side == "flat" and signal.reason and signal.reason != "hold":
        if not _defers(snapshot):
            _close_virtual(spec, pos, snapshot, now_ms, signal.reason)
        else:
            _defer(spec, snapshot, "exit", now_ms, reason=signal.reason)


def _open_virtual(
    spec: ShadowSetSpec,
    snapshot: PumpfunPaperSnapshot,
    now_ms: int,
    notional: float,
    impact_entry_bps: float,
    reason: str,
) -> None:
    quoted = quote_entry(snapshot, notional, _real_fees(snapshot))
    with _LOCK:
        if (spec.id, snapshot.symbol) in _opens:
            return
        if quoted is None:
            _record_decision(
                {
                    "ts": now_ms,
                    "set_id": spec.id,
                    "symbol": snapshot.symbol,
                    "mint": snapshot.mint,
                    "action": "skip",
                    "reason": "impact",
                    "habit_tag": spec.habit_tag,
                    "setup_seed_tags": list(spec.setup_seed_tags),
                },
                debounce=True,
            )
            return
        entry_price, tokens, sol_lamports = quoted
        _opens[(spec.id, snapshot.symbol)] = _Open(
            symbol=snapshot.symbol,
            mint=snapshot.mint,
            entry_price=entry_price,
            entry_ts=now_ms,
            entry_notional=float(notional),
            tokens=tokens,
            sol_lamports=sol_lamports,
            entry_impact_net_bps=_entry_net_bps(snapshot, notional, impact_entry_bps),
        )
        _last_open_ms[(spec.id, snapshot.mint)] = now_ms
        _record_decision(
            {
                "ts": now_ms,
                "set_id": spec.id,
                "symbol": snapshot.symbol,
                "mint": snapshot.mint,
                "action": "enter",
                "reason": reason,
                "habit_tag": spec.habit_tag,
                "setup_seed_tags": list(spec.setup_seed_tags),
            },
            debounce=False,
        )


def _close_virtual(
    spec: ShadowSetSpec,
    pos: _Open,
    snapshot: PumpfunPaperSnapshot,
    now_ms: int,
    reason: str,
) -> None:
    exit_sol = quote_exit_sol(snapshot, pos.tokens, _real_fees(snapshot))
    entry_sol = pos.sol_lamports / LAMPORTS_PER_SOL
    if exit_sol <= 0 and pos.tokens > 0:
        exit_sol = 0.0
    pnl, net_bps = hypothetical_net(entry_sol, exit_sol)
    tag = _REASON_TAG.get(reason, reason)
    row = {
        "set_id": spec.id,
        "symbol": pos.symbol,
        "mint": pos.mint,
        "entry_ts": pos.entry_ts,
        "exit_ts": now_ms,
        "entry_price": pos.entry_price,
        "exit_price": float(snapshot.price_sol),
        "pnl": pnl,
        "pnl_pct": (pnl / entry_sol) if entry_sol else 0.0,
        "net_bps": net_bps,
        "exit_reason": reason,
        "tags": [tag, "SHADOW", STRATEGY_ID],
        "habit_tag": spec.habit_tag,
        "setup_seed_tags": list(spec.setup_seed_tags),
        "entry_impact_net_bps": pos.entry_impact_net_bps,
        "market_source": _market_source(snapshot),
    }
    with _LOCK:
        current = _opens.get((spec.id, pos.symbol))
        if current is None or current.entry_ts != pos.entry_ts:
            return
        _opens.pop((spec.id, pos.symbol), None)
        _closed.append(row)
        if len(_closed) > JOURNAL_CAP:
            del _closed[: len(_closed) - JOURNAL_CAP + 200]
        _record_decision(
            {
                "ts": now_ms,
                "set_id": spec.id,
                "symbol": pos.symbol,
                "mint": pos.mint,
                "action": "exit",
                "reason": reason,
                "habit_tag": spec.habit_tag,
                "setup_seed_tags": list(spec.setup_seed_tags),
            },
            debounce=False,
        )
    _persist_shadow()
    try:
        from app.paper.events import note_shadow_close

        note_shadow_close(row)
    except Exception:
        pass


def shadow_open_for(symbol: str = "", mint: str = "") -> bool:
    """True when a shadow set still holds ``symbol`` or ``mint``."""
    sym = (symbol or "").strip()
    mid = (mint or "").strip()
    if not sym and not mid:
        return False
    _ensure_loaded()
    with _LOCK:
        for pos in _opens.values():
            if sym and pos.symbol == sym:
                return True
            if mid and pos.mint == mid:
                return True
        for (_sid, psym), pend in _pending.items():
            if sym and psym == sym:
                return True
            if mid and pend.get("mint") == mid:
                return True
    return False


def shadow_decisions() -> list[dict[str, Any]]:
    _ensure_loaded()
    with _LOCK:
        return [dict(row) for row in _decisions]


def shadow_closed(set_id: Optional[str] = None) -> list[dict[str, Any]]:
    _ensure_loaded()
    with _LOCK:
        rows = list(_closed)
    if set_id is None:
        return rows
    return [row for row in rows if row.get("set_id") == set_id]


def _median(vals: Sequence[float]) -> Optional[float]:
    if not vals:
        return None
    s = sorted(float(v) for v in vals)
    n = len(s)
    mid = n // 2
    if n % 2:
        return s[mid]
    return (s[mid - 1] + s[mid]) / 2.0


def _column(rows: Sequence[Mapping[str, Any]], *, ident: str, label: str) -> dict[str, Any]:
    pnls = [float(r["pnl"]) for r in rows if r.get("pnl") is not None]
    n = len(rows)
    wins = sum(1 for p in pnls if p > 0)
    nets = [float(r["net_bps"]) for r in rows if r.get("net_bps") is not None]
    return {
        "id": ident,
        "label": label,
        "n": n,
        "win_rate": (wins / n) if n else None,
        "expectancy": (sum(pnls) / n) if n else None,
        "median_net_bps": _median(nets),
        "sample_ok": n >= SAMPLE_MIN_N,
        "by_exit_reason": by_exit_reason(rows),
    }


def _counts_in_compare(row: Mapping[str, Any]) -> bool:
    from app.paper.ledger import NON_REAL_MARKET_SOURCES

    return str(row.get("market_source") or "") not in NON_REAL_MARKET_SOURCES


def _main_rows() -> list[dict[str, Any]]:
    from app.paper.ledger import excluded_from_go, get_paper_journal

    rows: list[dict[str, Any]] = []
    for trade in get_paper_journal().closed:
        if excluded_from_go(trade):
            continue
        if (trade.source or "") == "live":
            continue
        if (trade.source or "") == "manual":
            continue
        blob = " ".join(str(t).lower() for t in (trade.tags or []))
        if "source=manual" in blob or "source:manual" in blob:
            continue
        if trade.strategy_id != STRATEGY_ID and STRATEGY_ID not in (trade.tags or []):
            continue
        dumped = trade.as_dict()
        dumped["net_bps"] = float(trade.pnl_pct) * 1e4
        dumped["exit_reason"] = None
        rows.append(dumped)
    return rows


def _exit_vs_main(shadow_rows: Sequence[Mapping[str, Any]], main_rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    shadow_buckets = {b["reason"]: b for b in by_exit_reason(shadow_rows)}
    main_buckets = {b["reason"]: b for b in by_exit_reason(main_rows)}
    out: list[dict[str, Any]] = []
    for reason in shadow_buckets:
        s = shadow_buckets[reason]
        m = main_buckets.get(reason) or {"count": 0, "pct": 0.0}
        if int(s["count"]) == 0 and int(m["count"]) == 0:
            continue
        out.append(
            {
                "reason": reason,
                "main_count": int(m["count"]),
                "shadow_count": int(s["count"]),
                "main_pct": float(m["pct"]),
                "shadow_pct": float(s["pct"]),
            }
        )
    return out


def build_shadow_compare() -> dict[str, Any]:
    """Read-only report. Does not mutate journals, params, or live gates."""
    _ensure_loaded()
    with _LOCK:
        enabled = bool(_config.enabled)
        specs = list(_config.sets)
        closed = list(_closed)
    main_rows = [r for r in _main_rows() if _counts_in_compare(r)]
    main_col = _column(main_rows, ident="main", label="main")
    sets_out: list[dict[str, Any]] = []
    for spec in specs:
        rows = [r for r in closed if r.get("set_id") == spec.id and _counts_in_compare(r)]
        col = _column(rows, ident=spec.id, label=spec.label or spec.id)
        col.update(spec.as_public())
        col["exit_vs_main"] = _exit_vs_main(rows, main_rows)
        sets_out.append(col)
    try:
        main = _main_params()
        progress = {
            "progress_bps_min": int(main.progress_bps_min),
            "progress_bps_max": int(main.progress_bps_max),
        }
    except Exception:
        progress = {"progress_bps_min": 1500, "progress_bps_max": 6000}
    return {
        "enabled": enabled,
        "liveEnabled": False,
        "note": NOTE_NEVER_LIVE,
        "notes": [
            "paper only",
            NOTE_NEVER_LIVE,
            "does not change main Go window or executability",
            "setup_seed_tags are labels, not HabitProfile",
        ],
        "max_sets": MAX_SHADOW_SETS,
        "sample_min_n": SAMPLE_MIN_N,
        "go_window_label": GO_WINDOW_LABEL,
        "strategyId": STRATEGY_ID,
        "progress_locked": progress,
        "main": main_col,
        "sets": sets_out,
        "asof_ts": int(time.time() * 1000),
    }
