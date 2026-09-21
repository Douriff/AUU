"""Paper→live executability evidence (aggregations only).

Journal: n_closed / sample_ok / expectancy.
DecisionLog: impact median, shadow P50/P90, progress|impact|risk histogram.
Never enables live, never sends chain txs, never reads key material.
Locked: docs/adapters/decision-log-v0.md, docs/viz/executability-panel-v0.md
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping, Optional, Sequence

from app.models.contracts import Fill, OrderIntent, StrategyContext
from app.paper.decision_log import REJECT_BUCKETS, backfill_decision_shadows, classify_reject_bucket
from app.providers.pumpfun_curve_math import (
    AMM_IMPACT_FEE_FLOOR_BPS,
    CURVE_IMPACT_FEE_FLOOR_BPS,
    impact_venue_phase,
    split_impact_gross_fee_net,
)

THEORY_REF = "docs/research/executability-go-nogo-v0.md"
ADAPTER_REF = "docs/adapters/decision-log-v0.md"
PANEL_REF = "docs/viz/executability-panel-v0.md"
SHADOW_REF = "docs/research/shadow-fill-v0.md"

MIN_CLOSED_TRADES = 30
EXPECTANCY_MIN = 0.0
EXPECTANCY_TOLERANCE = 0.0  # v0: no approved negative slack
IMPACT_MEDIAN_MAX_BPS = 60.0
IMPACT_HARD_MAX_BPS = 80.0
SHADOW_SLIPPAGE_X_BPS = 40.0
PAPER_BROKER_LATENCY_MS = 300

LIVE_ENABLED = False
LIVE_LIMITS: dict[str, float | int] = {
    "max_notional_sol": 1.0,
    "max_day_loss_pct": 0.045,
    "max_open_mints": 10,
}
LIVE_GATE_REASON = "liveEnabled remains false until user secondary confirm + LiveLimits"

# Back-compat alias used by older tests; bucket name is now `progress`.
classify_reject_reason = classify_reject_bucket


def _as_mapping(row: Any) -> Mapping[str, Any]:
    if isinstance(row, Mapping):
        return row
    if hasattr(row, "model_dump"):
        return row.model_dump()
    if hasattr(row, "as_dict"):
        return row.as_dict()
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


def _pctile(vals: Sequence[float], p: float) -> Optional[float]:
    if not vals:
        return None
    s = sorted(float(v) for v in vals)
    idx = min(len(s) - 1, max(0, int(round((p / 100.0) * (len(s) - 1)))))
    return s[idx]


def shadow_slippage_bps(fill_price: float, quote: float) -> Optional[float]:
    if quote is None or quote <= 0 or fill_price is None:
        return None
    return abs(float(fill_price) - float(quote)) / float(quote) * 1e4


def annotate_fill_executability(
    fill: Fill, ctx: StrategyContext, intent: OrderIntent
) -> Fill:
    """Attach quote / curve impact gross+fee+net / shadow slippage. Does not change fill price."""
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
    if impact is None and intent.side == "buy" and ctx.meta:
        hint = ctx.meta.get("entry_impact_bps")
        if hint is not None:
            try:
                impact = float(hint)
            except (TypeError, ValueError):
                impact = None
    phase_hint = ctx.meta.get("phase") if ctx.meta else None
    phase = impact_venue_phase(ctx.pump, str(phase_hint) if phase_hint else None)
    addon = None
    if ctx.liquidity.fee_bps is not None:
        addon = float(ctx.liquidity.fee_bps)
    elif ctx.pump is not None and getattr(ctx.pump, "fee_bps", None) is not None:
        addon = float(ctx.pump.fee_bps)
    gross, fee, net = split_impact_gross_fee_net(
        impact, phase=phase, impact_fee_bps=addon
    )
    shadow = shadow_slippage_bps(float(fill.price), quote) if quote else None
    if shadow is None and fill.slippage_bps is not None:
        shadow = float(fill.slippage_bps)
    return fill.model_copy(
        update={
            "quote_price": quote,
            "estimated_impact_bps": gross,
            "estimated_impact_gross_bps": gross,
            "estimated_impact_net_bps": net,
            "protocol_fee_bps": fee,
            "shadow_slippage_bps": shadow,
        }
    )


def _live_checks() -> dict[str, bool]:
    """Readonly snapshot of live-ui-gates. Never sets liveEnabled."""
    empty = {
        "live_limits": False,
        "keypair_mounted": False,
        "secondary_confirm": False,
    }
    try:
        from app.live.gate import evaluate

        st = evaluate()
        limits_ok = not list(getattr(st, "limits_missing", []) or [])
        return {
            "live_limits": bool(limits_ok),
            "keypair_mounted": bool(getattr(st, "keypair_configured", False)),
            "secondary_confirm": bool(getattr(st, "live_confirmed", False)),
        }
    except Exception:
        return empty


def _frozen_live_limits() -> dict[str, float | int]:
    caps = dict(LIVE_LIMITS)
    try:
        from app.live.gate import effective_limits

        lim = effective_limits().as_dict()
        caps = {
            "max_notional_sol": min(float(lim["max_notional_sol"]), float(LIVE_LIMITS["max_notional_sol"])),
            "max_day_loss_pct": min(float(lim["max_day_loss_pct"]), float(LIVE_LIMITS["max_day_loss_pct"])),
            "max_open_mints": min(int(lim["max_open_mints"]), int(LIVE_LIMITS["max_open_mints"])),
        }
    except Exception:
        pass
    return caps


def _gate(ok: bool, **extra: Any) -> dict[str, Any]:
    payload = {"ok": bool(ok)}
    payload.update(extra)
    return payload


def _reject_from_log(log_rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    by_bucket = {k: 0 for k in REJECT_BUCKETS}
    by_reason: dict[str, int] = {}
    n_eval = 0
    for row in log_rows:
        outcome = str(row.get("outcome") or "")
        stage = str(row.get("stage") or "")
        bucket = str(row.get("reject_bucket") or "none")
        if bucket == "progress_band":
            bucket = "progress"
        reason = str(row.get("signal_reason") or "")
        tags = list(row.get("signal_tags") or []) + list(row.get("risk_tags") or [])
        if not bucket or bucket == "none":
            bucket = classify_reject_bucket(reason, tags)
        # Count signal-stage and rejected pre_order/live/paper rows as evals.
        counted = False
        if stage == "signal" or outcome == "reject":
            n_eval += 1
            counted = True
        elif outcome in {"fill", "partial", "emit_signal"}:
            n_eval += 1
            counted = True
        if not counted:
            continue
        if reason:
            by_reason[reason] = by_reason.get(reason, 0) + 1
        if bucket in by_bucket and outcome == "reject":
            by_bucket[bucket] += 1
    denom = max(n_eval, 0)
    by: dict[str, Any] = {}
    for k in REJECT_BUCKETS:
        count = by_bucket[k]
        by[k] = {"count": count, "rate": (count / denom) if denom else 0.0}
    return {
        "n_entry_evals": n_eval,
        "by_reason": by,
        "reasons": by_reason,
        "ok": all(k in by for k in REJECT_BUCKETS),
    }


def _resolve_impact(row: Mapping[str, Any]) -> Optional[tuple[float, float, float]]:
    """(gross, protocol_fee, net) for one entry. None when gross was never recorded."""
    gross = _f(
        row,
        "impact_gross_bps",
        "entry_estimated_impact_gross_bps",
        "estimated_impact_gross_bps",
        "impact_bps_est",
        "estimated_impact_bps",
        "entry_estimated_impact_bps",
    )
    if gross is None:
        return None
    fee = _f(row, "entry_protocol_fee_bps", "protocol_fee_bps")
    net = _f(
        row,
        "entry_estimated_impact_net_bps",
        "estimated_impact_net_bps",
        "impact_net_bps",
    )
    phase = str(row.get("entry_phase") or row.get("phase") or "curve")
    g, fee_v, net_v = split_impact_gross_fee_net(
        gross,
        protocol_fee_bps=fee,
        net_bps=net,
        phase=phase,
        impact_fee_bps=_f(row, "impact_fee_bps", "fee_bps"),
    )
    if g is None or fee_v is None or net_v is None:
        return None
    return g, fee_v, net_v


def _is_entry_fill(row: Mapping[str, Any]) -> bool:
    outcome = str(row.get("outcome") or "")
    if outcome and outcome not in {"fill", "partial"}:
        return False
    side = str(row.get("signal_side") or row.get("side") or "").lower()
    if side in {"short", "sell"}:
        return False
    qty = _f(row, "qty")
    if qty is not None and qty < 0:
        return False
    return True


def _index_entry_impacts(
    rows: Sequence[Mapping[str, Any]],
) -> dict[str, list[tuple[int, tuple[float, float, float]]]]:
    by: dict[str, list[tuple[int, tuple[float, float, float]]]] = {}
    for row in rows:
        if not _is_entry_fill(row):
            continue
        resolved = _resolve_impact(row)
        if resolved is None:
            continue
        sym = str(row.get("symbol") or "")
        ts = int(row.get("ts") or row.get("entry_ts") or 0)
        by.setdefault(sym, []).append((ts, resolved))
    return by


def _take_closest(
    bucket: dict[str, list[tuple[int, tuple[float, float, float]]]],
    symbol: str,
    entry_ts: int,
) -> Optional[tuple[float, float, float]]:
    cands = bucket.get(symbol) or []
    if not cands:
        return None
    best_i = min(range(len(cands)), key=lambda i: abs(cands[i][0] - int(entry_ts)))
    return cands.pop(best_i)[1]


def _entry_impacts(
    log_rows: Sequence[Mapping[str, Any]],
    trade_rows: Sequence[Mapping[str, Any]],
    fill_rows: Optional[Sequence[Mapping[str, Any]]] = None,
) -> list[tuple[float, float, float]]:
    """One (gross, fee, net) per closed trade.

    Journal closes are the sample. A short DecisionLog must not replace them
    (that reported n=3 while n_closed=30 and then failed the <60 net gate).
    Missing journal fields are filled from an already-recorded entry fill or
    DecisionLog row for the same symbol. Nothing here invents an impact.
    """
    if trade_rows:
        logs = _index_entry_impacts(log_rows)
        fills = _index_entry_impacts(fill_rows or [])
        out: list[tuple[float, float, float]] = []
        for row in trade_rows:
            resolved = _resolve_impact(row)
            if resolved is None:
                ts = int(row.get("entry_ts") or row.get("ts") or 0)
                sym = str(row.get("symbol") or "")
                resolved = _take_closest(fills, sym, ts) or _take_closest(logs, sym, ts)
            if resolved is not None:
                out.append(resolved)
        return out
    out = []
    for row in log_rows:
        if str(row.get("outcome") or "") not in {"fill", "partial"}:
            continue
        if not _is_entry_fill(row):
            continue
        resolved = _resolve_impact(row)
        if resolved is not None:
            out.append(resolved)
    return out


def _shadows_from_log(log_rows: Sequence[Mapping[str, Any]]) -> list[float]:
    out: list[float] = []
    for row in log_rows:
        if str(row.get("outcome") or "") not in {"fill", "partial"}:
            continue
        v = _f(row, "shadow_slippage_bps")
        if v is not None:
            out.append(v)
    return out


def _errors_from_log(log_rows: Sequence[Mapping[str, Any]]) -> list[float]:
    out: list[float] = []
    for row in log_rows:
        if str(row.get("outcome") or "") not in {"fill", "partial"}:
            continue
        v = _f(row, "impact_error_bps")
        if v is not None:
            out.append(v)
            continue
        shadow = _f(row, "shadow_slippage_bps")
        est = _f(row, "impact_bps_est", "estimated_impact_bps")
        if shadow is not None and est is not None:
            out.append(float(shadow) - float(est))
    return out


def _nogo_reason(
    *,
    lamp: str,
    n: int,
    sample_ok: bool,
    expectancy: Optional[float],
    median_impact: Optional[float],
    median_net: Optional[float],
    max_gross: Optional[float],
    shadow_p50: Optional[float],
    gates: Mapping[str, Mapping[str, Any]],
) -> str:
    if lamp == "green":
        return "过门槛（实盘仍关）"
    if not sample_ok:
        return f"平仓样本 {n}/30"
    if expectancy is None:
        return "证据不足"
    if not gates["expectancy"]["ok"]:
        return f"期望 {expectancy:.4g} < 0"
    if median_impact is None or not gates["median_entry_impact"]["ok"]:
        if median_impact is None or median_net is None:
            return "证据不足：缺少入场冲击样本"
        hard_fail = max_gross is not None and max_gross > IMPACT_HARD_MAX_BPS
        net_fail = median_net >= IMPACT_MEDIAN_MAX_BPS
        if net_fail and hard_fail:
            return (
                f"入场冲击扣费中位 {median_net:.1f} bps（门槛 <60）；"
                f"含费最大 {max_gross:.1f} 超硬顶 80"
            )
        if hard_fail:
            return f"入场冲击含费 {max_gross:.1f} bps 超硬顶 80"
        return f"入场冲击扣费中位 {median_net:.1f} bps（门槛 <60）"
    if shadow_p50 is None or not gates["shadow_slippage"]["ok"]:
        if shadow_p50 is None:
            return "证据不足：缺少影子滑点样本"
        return f"影子滑点 P50 {shadow_p50:.1f} bps"
    if not gates["reject_rate"]["ok"]:
        return "拒单结构未报告"
    return "证据不足"


def aggregate_executability(
    trades: Iterable[Any],
    *,
    fills: Optional[Iterable[Any]] = None,
    decisions: Optional[Iterable[Any]] = None,
    eval_counts: Optional[Mapping[str, Any]] = None,
    decision_log: Optional[Iterable[Any]] = None,
    window: str | int = "session",
    params_max_impact_bps: Optional[float] = None,
) -> dict[str, Any]:
    """Pure aggregation. No I/O, no chain, no secrets."""
    trade_rows = [_as_mapping(t) for t in trades]
    log_rows = [_as_mapping(r) for r in (decision_log or [])]
    if not log_rows and decisions:
        # Adapt AutoDecision / eval fixtures into DecisionLog-shaped rows.
        for d in decisions:
            m = _as_mapping(d)
            reason = str(m.get("reason") or m.get("signal_reason") or "")
            tags = list(m.get("tags") or m.get("signal_tags") or [])
            action = str(m.get("action") or m.get("outcome") or "")
            outcome = m.get("outcome")
            if not outcome:
                if action in {"fill"}:
                    outcome = "fill"
                elif action in {"submit"}:
                    outcome = "emit_signal"
                else:
                    outcome = "reject"
            bucket = m.get("reject_bucket") or classify_reject_bucket(reason, tags)
            if bucket == "progress_band":
                bucket = "progress"
            log_rows.append(
                {
                    "ts": m.get("ts") or 0,
                    "strategy_id": m.get("strategy_id") or "pump-paper-v1",
                    "symbol": m.get("symbol") or "",
                    "stage": m.get("stage") or ("paper_submit" if outcome == "fill" else "signal"),
                    "signal_side": m.get("signal_side") or ("long" if outcome != "reject" else "flat"),
                    "signal_reason": reason,
                    "signal_tags": tags,
                    "risk_tags": list(m.get("risk_tags") or tags),
                    "outcome": outcome,
                    "reject_bucket": bucket,
                    "impact_bps_est": m.get("impact_bps_est"),
                    "impact_gross_bps": m.get("impact_gross_bps"),
                    "protocol_fee_bps": m.get("protocol_fee_bps"),
                    "phase": m.get("phase"),
                    "impact_fee_bps": m.get("impact_fee_bps") or m.get("fee_bps"),
                    "shadow_slippage_bps": m.get("shadow_slippage_bps"),
                }
            )
    if eval_counts and not log_rows:
        # Expand counters into synthetic reject rows so histogram still reports.
        raw_bucket = eval_counts.get("by_bucket") or {}
        for old, new in (("progress_band", "progress"), ("progress", "progress"), ("impact", "impact"), ("risk", "risk")):
            n = int(raw_bucket.get(old) or 0)
            for _i in range(n):
                log_rows.append(
                    {
                        "stage": "signal",
                        "outcome": "reject",
                        "reject_bucket": new,
                        "signal_reason": new,
                    }
                )
        for _i in range(int(raw_bucket.get("attempt") or 0)):
            log_rows.append({"stage": "signal", "outcome": "emit_signal", "reject_bucket": "none"})

    n = len(trade_rows)
    pnls = [_f(t, "pnl") for t in trade_rows]
    pnls_f = [p for p in pnls if p is not None]
    expectancy = (sum(pnls_f) / n) if n and len(pnls_f) == n else (
        (sum(pnls_f) / len(pnls_f)) if pnls_f else None
    )
    sample_ok = n >= MIN_CLOSED_TRADES
    exp_floor = EXPECTANCY_MIN + EXPECTANCY_TOLERANCE
    expectancy_ok = expectancy is not None and sample_ok and expectancy >= exp_floor

    fill_rows = [_as_mapping(f) for f in (fills or [])]
    impact_pairs = _entry_impacts(log_rows, trade_rows, fill_rows)
    impacts = [g for g, _fee, _net in impact_pairs]
    nets = [net for _g, _fee, net in impact_pairs]
    fees = [fee for _g, fee, _net in impact_pairs]
    median_impact = _median(impacts)
    median_net = _median(nets)
    median_fee = _median(fees)
    max_impact = max(impacts) if impacts else None
    impact_n = len(impacts)
    impact_coverage_ok = impact_n >= MIN_CLOSED_TRADES if n >= MIN_CLOSED_TRADES else bool(impacts) and n > 0
    if n == 0:
        impact_coverage_ok = False
    # Go median is net of protocol fee. Hard ceiling stays on gross.
    median_ok = (
        median_net is not None
        and impact_coverage_ok
        and median_net < IMPACT_MEDIAN_MAX_BPS
        and (max_impact is None or max_impact <= IMPACT_HARD_MAX_BPS)
    )
    protocol_fee_bps = (
        float(median_fee) if median_fee is not None else float(CURVE_IMPACT_FEE_FLOOR_BPS)
    )

    shadows = _shadows_from_log(log_rows)
    if not shadows:
        for t in trade_rows:
            v = _f(t, "entry_shadow_slippage_bps", "shadow_slippage_bps")
            if v is not None:
                shadows.append(v)
    shadow_p50 = _median(shadows)
    shadow_p90 = _pctile(shadows, 90)
    shadow_n = len(shadows)
    shadow_coverage_ok = shadow_n >= MIN_CLOSED_TRADES if n >= MIN_CLOSED_TRADES else bool(shadows) and n > 0
    if n == 0:
        shadow_coverage_ok = False
    shadow_ok = (
        shadow_p50 is not None
        and shadow_coverage_ok
        and shadow_p50 <= SHADOW_SLIPPAGE_X_BPS
    )

    errors = _errors_from_log(log_rows)
    error_p50 = _median(errors)
    error_p90 = _pctile(errors, 90)

    reject = _reject_from_log(log_rows)

    missing_data = (not sample_ok) or (n > 0 and (not impact_coverage_ok or not shadow_coverage_ok))
    paper_go = (
        sample_ok
        and bool(expectancy_ok)
        and bool(median_ok)
        and bool(shadow_ok)
        and bool(reject["ok"])
    )
    if paper_go:
        lamp = "green"
    elif missing_data:
        lamp = "gray"
    else:
        lamp = "red"

    live_checks = _live_checks()
    live_limits = _frozen_live_limits()

    gates = {
        "sample_ok": _gate(
            sample_ok,
            n_closed=n,
            n_trades=n,
            min=MIN_CLOSED_TRADES,
            note=None if sample_ok else "样本不足",
        ),
        "expectancy": _gate(
            bool(expectancy_ok),
            value=expectancy,
            min=exp_floor,
            tolerance=EXPECTANCY_TOLERANCE,
            warn_negative=bool(expectancy is not None and expectancy < 0),
        ),
        "median_entry_impact": _gate(
            bool(median_ok),
            median_bps=median_net,
            median_gross_bps=median_impact,
            median_net_bps=median_net,
            protocol_fee_bps=protocol_fee_bps,
            max_bps=max_impact,
            max_gross_bps=max_impact,
            n=impact_n,
            go_max=IMPACT_MEDIAN_MAX_BPS,
            hard_max=IMPACT_HARD_MAX_BPS,
            basis="net_of_protocol_fee",
        ),
        "reject_rate": _gate(
            bool(reject["ok"]),
            n_entry_evals=reject["n_entry_evals"],
            by_reason=reject["by_reason"],
            reasons=reject["reasons"],
        ),
        "shadow_slippage": _gate(
            bool(shadow_ok),
            p50_bps=shadow_p50,
            p90_bps=shadow_p90,
            median_bps=shadow_p50,
            x_bps=SHADOW_SLIPPAGE_X_BPS,
            n=shadow_n,
        ),
        "live": _gate(
            False,
            liveEnabled=LIVE_ENABLED,
            reason=LIVE_GATE_REASON,
            live_limits=live_limits,
            checks=live_checks,
        ),
    }

    nogo_reason = _nogo_reason(
        lamp=lamp,
        n=n,
        sample_ok=sample_ok,
        expectancy=expectancy,
        median_impact=median_impact,
        median_net=median_net,
        max_gross=max_impact,
        shadow_p50=shadow_p50,
        gates=gates,
    )

    return {
        "mode": "paper",
        "verdict": "go" if paper_go else "no-go",
        "lamp": lamp,
        "nogo_reason": nogo_reason,
        "liveEnabled": False,
        "liveDisabled": True,
        "live_limits": live_limits,
        "live_checks": live_checks,
        "window": window,
        "n_closed": n,
        "n_trades": n,
        "sample_ok": sample_ok,
        "expectancy": expectancy,
        "median_entry_impact_bps": median_impact,
        "median_entry_impact_gross_bps": median_impact,
        "median_entry_impact_net_bps": median_net,
        "protocol_fee_bps": protocol_fee_bps,
        "protocol_fee_bps_curve": float(CURVE_IMPACT_FEE_FLOOR_BPS),
        "protocol_fee_bps_amm": float(AMM_IMPACT_FEE_FLOOR_BPS),
        "max_entry_impact_bps": max_impact,
        "max_entry_impact_gross_bps": max_impact,
        "impact_cap_go_bps": IMPACT_MEDIAN_MAX_BPS,
        "hard_max_impact_bps": IMPACT_HARD_MAX_BPS,
        "params_max_impact_bps": params_max_impact_bps,
        "reject_rate": reject["by_reason"],
        "reject_reasons": reject["reasons"],
        "n_entry_evals": reject["n_entry_evals"],
        "shadow_slippage": {
            "p50_bps": shadow_p50,
            "p90_bps": shadow_p90,
            "median_bps": shadow_p50,
            "x_bps": SHADOW_SLIPPAGE_X_BPS,
            "n": shadow_n,
            "ok": bool(shadow_ok),
        },
        "impact_error": {
            "p50_bps": error_p50,
            "p90_bps": error_p90,
            "n": len(errors),
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
        "adapter_ref": ADAPTER_REF,
        "panel_ref": PANEL_REF,
        "shadow_ref": SHADOW_REF,
        "disclaimer": "paper executability evidence, not a live fill guarantee — 纸面可执行性证据，非实盘承诺",
        "empty": n == 0,
    }


LOG_QUERY = 2_000


def build_executability(
    *,
    window: str = "session",
    from_ts: Optional[int] = None,
    to_ts: Optional[int] = None,
) -> dict[str, Any]:
    from app.paper.decision_log import get_decision_log
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
    log = get_decision_log()
    rows = log.all() if n_window is None else log.query(from_ts=from_ts, to_ts=to_ts, limit=LOG_QUERY)
    backfill_decision_shadows(rows)
    return aggregate_executability(
        trades,
        fills=journal.fills,
        decision_log=rows,
        window=window_label,
        params_max_impact_bps=float(engine.params.max_impact_bps),
    )

