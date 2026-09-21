"""Paper→live executability evidence (aggregations only).

Never enables live, never sends chain txs, never reads key material.
Thresholds locked in docs/research/executability-go-nogo-v0.md.
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping, Optional, Sequence

from app.models.contracts import Fill, OrderIntent, StrategyContext

THEORY_REF = "docs/research/executability-go-nogo-v0.md"

# G1 — stricter than PaperStats MIN_SAMPLE_OK (20)
MIN_CLOSED_TRADES = 30

# G2
EXPECTANCY_MIN = 0.0
EXPECTANCY_TOLERANCE = 0.0  # v0: no approved negative slack

# G3 — median go-cap 60; strategy/distill hard max still 80
IMPACT_MEDIAN_MAX_BPS = 60.0
IMPACT_HARD_MAX_BPS = 80.0

# G5
SHADOW_SLIPPAGE_X_BPS = 40.0

# PaperBroker delay (same ctx; not slot/Jito)
PAPER_BROKER_LATENCY_MS = 300

# G6 — dark. Do not raise any of these (raising = weakening).
LIVE_ENABLED = False
LIVE_LIMITS: dict[str, float | int] = {
    "max_notional_sol": 1.0,
    "max_day_loss_pct": 0.045,
    "max_open_mints": 10,
}
LIVE_GATE_REASON = "liveEnabled remains false until user secondary confirm + LiveLimits"

REJECT_BUCKETS = ("progress_band", "impact", "risk")

PROGRESS_BAND_REASONS = frozenset({"progress_band", "not_curve"})
IMPACT_REASONS = frozenset({"impact"})
IMPACT_TAGS = frozenset({"SLIPPAGE_CAP"})
# User-switch skip is not a market reject.
IGNORE_REASONS = frozenset({"auto_paper_orders=false", "hold"})
IGNORE_TAGS = frozenset({"AUTOPAPER_OFF"})

RISK_REASONS = frozenset(
    {
        "blocked_tag",
        "reject_cooldown",
        "cooldown",
        "max_open_mints",
        "live_disabled",
        "trading_halted",
        "reduce_only",
        "day_loss_breaker",
        "honeypot_flag",
        "tax_high",
        "spread_too_wide",
        "depth_thin",
        "position_cap",
        "max_notional",
        "overfill",
        "dup_fill",
        "curve_near_graduation",
        "risk_denied",
    }
)
RISK_TAGS = frozenset(
    {
        "LIVE_DISABLED",
        "TRADING_HALTED",
        "REDUCE_ONLY",
        "DAY_LOSS_BREAKER",
        "HONEYPOT_FLAG",
        "TAX_HIGH",
        "SPREAD_TOO_WIDE",
        "DEPTH_THIN",
        "POSITION_CAP",
        "COOLDOWN",
        "MAX_NOTIONAL",
        "OVERFILL",
        "DUP_FILL",
        "CURVE_NEAR_GRADUATION",
        "RISK_DENIED",
        "MEV_SUSPECT",
    }
)


def classify_reject_reason(reason: str = "", tags: Optional[Sequence[str]] = None) -> Optional[str]:
    """Map a strategy/RiskGate reason into progress_band | impact | risk, else None."""
    tagset = {str(t).upper() for t in (tags or []) if t}
    if tagset & IGNORE_TAGS:
        return None
    raw = (reason or "").strip()
    lowered = raw.lower()
    if lowered in IGNORE_REASONS:
        return None
    if lowered in PROGRESS_BAND_REASONS:
        return "progress_band"
    if lowered in IMPACT_REASONS or (tagset & IMPACT_TAGS):
        return "impact"
    if lowered in RISK_REASONS or (tagset & RISK_TAGS):
        return "risk"
    return None


def _as_mapping(row: Any) -> Mapping[str, Any]:
    if isinstance(row, Mapping):
        return row
    if hasattr(row, "as_dict"):
        return row.as_dict()
    if hasattr(row, "model_dump"):
        return row.model_dump()
    return {
        k: getattr(row, k)
        for k in dir(row)
        if not k.startswith("_") and not callable(getattr(row, k, None))
    }


def _f(row: Mapping[str, Any], *keys: str) -> Optional[float]:
    for k in keys:
        if k in row and row[k] is not None:
            try:
                return float(row[k])
            except (TypeError, ValueError):
                continue
    return None


def _median(vals: Sequence[float]) -> Optional[float]:
    if not vals:
        return None
    s = sorted(float(v) for v in vals)
    n = len(s)
    mid = n // 2
    if n % 2:
        return s[mid]
    return (s[mid - 1] + s[mid]) / 2.0


def shadow_slippage_bps(fill_price: float, quote: float) -> Optional[float]:
    if quote is None or quote <= 0 or fill_price is None:
        return None
    return abs(float(fill_price) - float(quote)) / float(quote) * 1e4


def annotate_fill_executability(
    fill: Fill, ctx: StrategyContext, intent: OrderIntent
) -> Fill:
    """Attach quote / curve impact / shadow slippage. Does not change fill price."""
    quote = None
    if ctx.tick and ctx.tick.mid and float(ctx.tick.mid) > 0:
        quote = float(ctx.tick.mid)
    notional = abs(float(intent.qty_or_notional))
    impact = None
    try:
        impact = float(
            ctx.liquidity.estimated_impact_bps(
                notional, side=intent.side, pump=ctx.pump
            )
        )
    except (ValueError, TypeError, ZeroDivisionError):
        impact = None
    shadow = shadow_slippage_bps(float(fill.price), quote) if quote else None
    if shadow is None and fill.slippage_bps is not None:
        shadow = float(fill.slippage_bps)
    return fill.model_copy(
        update={
            "quote_price": quote,
            "estimated_impact_bps": impact,
            "shadow_slippage_bps": shadow,
        }
    )


def _entry_impacts(trades: Sequence[Mapping[str, Any]]) -> list[float]:
    out: list[float] = []
    for t in trades:
        v = _f(t, "entry_estimated_impact_bps", "estimated_impact_bps")
        if v is not None:
            out.append(v)
    return out


def _entry_shadows(
    trades: Sequence[Mapping[str, Any]], fills: Sequence[Mapping[str, Any]]
) -> list[float]:
    out: list[float] = []
    for t in trades:
        v = _f(t, "entry_shadow_slippage_bps", "shadow_slippage_bps")
        if v is None:
            quote = _f(t, "entry_quote_price", "quote_price")
            px = _f(t, "entry_price")
            if quote is not None and px is not None:
                v = shadow_slippage_bps(px, quote)
        if v is not None:
            out.append(v)
    if out:
        return out
    # Fallback: opening fills (qty > 0) if journal extras exist.
    for f in fills:
        qty = _f(f, "qty")
        if qty is not None and qty <= 0:
            continue
        v = _f(f, "shadow_slippage_bps")
        if v is None:
            quote = _f(f, "quote_price")
            px = _f(f, "price")
            if quote is not None and px is not None:
                v = shadow_slippage_bps(px, quote)
        if v is not None:
            out.append(v)
    return out


def _reject_from_eval(
    eval_counts: Optional[Mapping[str, Any]],
    decisions: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    by_bucket = {k: 0 for k in REJECT_BUCKETS}
    by_reason: dict[str, int] = {}
    total = 0
    if eval_counts:
        total = int(eval_counts.get("total") or 0)
        raw_bucket = eval_counts.get("by_bucket") or {}
        for k in REJECT_BUCKETS:
            by_bucket[k] = int(raw_bucket.get(k) or 0)
        for reason, n in (eval_counts.get("by_reason") or {}).items():
            by_reason[str(reason)] = int(n)
    elif decisions:
        for d in decisions:
            action = str(d.get("action") or "")
            if action not in {"skip", "deny", "refuse"}:
                if action in {"fill", "submit"}:
                    total += 1
                continue
            reason = str(d.get("reason") or "")
            tags = list(d.get("tags") or [])
            if classify_reject_reason(reason, tags) is None and (
                reason.lower() in IGNORE_REASONS or set(tags) & IGNORE_TAGS
            ):
                continue
            total += 1
            by_reason[reason or "unknown"] = by_reason.get(reason or "unknown", 0) + 1
            bucket = classify_reject_reason(reason, tags)
            if bucket in by_bucket:
                by_bucket[bucket] += 1
    denom = max(total, 0)
    by: dict[str, Any] = {}
    for k in REJECT_BUCKETS:
        count = by_bucket[k]
        by[k] = {
            "count": count,
            "rate": (count / denom) if denom else 0.0,
        }
    return {
        "n_entry_evals": total,
        "by_reason": by,
        "reasons": by_reason,
        "reported": True,
        "ok": all(k in by for k in REJECT_BUCKETS),
    }


def _gate(ok: bool, **extra: Any) -> dict[str, Any]:
    payload = {"ok": bool(ok)}
    payload.update(extra)
    return payload


def aggregate_executability(
    trades: Iterable[Any],
    *,
    fills: Optional[Iterable[Any]] = None,
    decisions: Optional[Iterable[Any]] = None,
    eval_counts: Optional[Mapping[str, Any]] = None,
    window: str | int = "session",
    params_max_impact_bps: Optional[float] = None,
) -> dict[str, Any]:
    """Pure aggregation. No I/O, no chain, no secrets."""
    trade_rows = [_as_mapping(t) for t in trades]
    fill_rows = [_as_mapping(f) for f in (fills or [])]
    decision_rows = [_as_mapping(d) for d in (decisions or [])]

    n = len(trade_rows)
    pnls = [_f(t, "pnl") for t in trade_rows]
    pnls_f = [p for p in pnls if p is not None]
    expectancy = (sum(pnls_f) / n) if n and len(pnls_f) == n else (
        (sum(pnls_f) / len(pnls_f)) if pnls_f else None
    )

    sample_ok = n >= MIN_CLOSED_TRADES
    exp_floor = EXPECTANCY_MIN + EXPECTANCY_TOLERANCE
    expectancy_ok = expectancy is not None and sample_ok and expectancy >= exp_floor

    impacts = _entry_impacts(trade_rows)
    median_impact = _median(impacts)
    max_impact = max(impacts) if impacts else None
    impact_n = len(impacts)
    impact_coverage_ok = impact_n >= min(n, MIN_CLOSED_TRADES) if n else False
    if n >= MIN_CLOSED_TRADES:
        impact_coverage_ok = impact_n >= MIN_CLOSED_TRADES
    median_ok = (
        median_impact is not None
        and impact_coverage_ok
        and median_impact < IMPACT_MEDIAN_MAX_BPS
        and (max_impact is None or max_impact <= IMPACT_HARD_MAX_BPS)
    )

    shadows = _entry_shadows(trade_rows, fill_rows)
    median_shadow = _median(shadows)
    shadow_n = len(shadows)
    shadow_coverage_ok = shadow_n >= MIN_CLOSED_TRADES if n >= MIN_CLOSED_TRADES else shadow_n >= n and n > 0
    if n == 0:
        shadow_coverage_ok = False
    shadow_ok = (
        median_shadow is not None
        and shadow_coverage_ok
        and median_shadow <= SHADOW_SLIPPAGE_X_BPS
    )

    reject = _reject_from_eval(eval_counts, decision_rows)

    gates = {
        "sample_ok": _gate(
            sample_ok,
            n_trades=n,
            min=MIN_CLOSED_TRADES,
            note=None if sample_ok else "样本不足",
        ),
        "expectancy": _gate(
            bool(expectancy_ok),
            value=expectancy,
            min=exp_floor,
            tolerance=EXPECTANCY_TOLERANCE,
        ),
        "median_entry_impact": _gate(
            bool(median_ok),
            median_bps=median_impact,
            max_bps=max_impact,
            n=impact_n,
            go_max=IMPACT_MEDIAN_MAX_BPS,
            hard_max=IMPACT_HARD_MAX_BPS,
        ),
        "reject_rate": _gate(
            bool(reject["ok"]),
            n_entry_evals=reject["n_entry_evals"],
            by_reason=reject["by_reason"],
            reasons=reject["reasons"],
        ),
        "shadow_slippage": _gate(
            bool(shadow_ok),
            median_bps=median_shadow,
            x_bps=SHADOW_SLIPPAGE_X_BPS,
            n=shadow_n,
        ),
        "live": _gate(
            False,
            liveEnabled=LIVE_ENABLED,
            reason=LIVE_GATE_REASON,
            live_limits=dict(LIVE_LIMITS),
        ),
    }

    paper_go = all(
        gates[k]["ok"]
        for k in (
            "sample_ok",
            "expectancy",
            "median_entry_impact",
            "reject_rate",
            "shadow_slippage",
        )
    )

    return {
        "mode": "paper",
        "verdict": "go" if paper_go else "no-go",
        "liveEnabled": False,
        "liveDisabled": True,
        "live_limits": dict(LIVE_LIMITS),
        "window": window,
        "n_trades": n,
        "sample_ok": sample_ok,
        "expectancy": expectancy,
        "median_entry_impact_bps": median_impact,
        "max_entry_impact_bps": max_impact,
        "impact_cap_go_bps": IMPACT_MEDIAN_MAX_BPS,
        "hard_max_impact_bps": IMPACT_HARD_MAX_BPS,
        "params_max_impact_bps": params_max_impact_bps,
        "reject_rate": reject["by_reason"],
        "reject_reasons": reject["reasons"],
        "n_entry_evals": reject["n_entry_evals"],
        "shadow_slippage": {
            "median_bps": median_shadow,
            "x_bps": SHADOW_SLIPPAGE_X_BPS,
            "n": shadow_n,
            "ok": bool(shadow_ok),
        },
        "latency": {
            "paper_broker_latency_ms": PAPER_BROKER_LATENCY_MS,
            "modeled_slot_ms": False,
            "note": "paper delays ts only; same ctx quote — not slot/Jito landing",
        },
        "mev": {
            "modeled": False,
            "note": "paper fills are not sandwiched; residual live risk",
        },
        "gates": gates,
        "theory_ref": THEORY_REF,
        "disclaimer": "paper executability evidence, not a live fill guarantee — 纸面可执行性证据，非实盘承诺",
        "empty": n == 0,
    }


def build_executability(
    *,
    window: str = "session",
    from_ts: Optional[int] = None,
    to_ts: Optional[int] = None,
) -> dict[str, Any]:
    from app.paper.ledger import get_paper_journal
    from app.strategies.pump_paper_v1 import get_engine

    journal = get_paper_journal()
    engine = get_engine()
    n_window: Optional[int] = None
    window_label: str | int = "session"
    raw = str(window).strip().lower()
    if raw not in {"", "session", "all"}:
        try:
            n_window = max(1, int(raw))
            window_label = n_window
        except ValueError:
            window_label = "session"
    trades = journal.closed_in_window(window=n_window, from_ts=from_ts, to_ts=to_ts)
    return aggregate_executability(
        trades,
        fills=journal.fills,
        decisions=engine.last_decisions(200),
        eval_counts=engine.eval_snapshot(),
        window=window_label,
        params_max_impact_bps=float(engine.params.max_impact_bps),
    )
