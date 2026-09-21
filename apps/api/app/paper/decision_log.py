"""DecisionLog — paper reject / fill evidence rows.

Locked: docs/adapters/decision-log-v0.md
Buckets: progress | impact | risk | none
Never enables live, never sends chain txs.
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping, Optional, Sequence

from app.models.contracts import DecisionLogRow, Fill, RiskOut, SignalOut
from app.providers.pumpfun_curve_math import (
    impact_net_bps as compute_impact_net_bps,
    impact_venue_phase,
    protocol_fee_bps_for_phase,
)

LOG_CAP = 2_000

# Spec §1 buckets
PROGRESS_REASONS = frozenset({"progress_band", "not_curve"})
IMPACT_REASONS = frozenset({"impact"})
IMPACT_TAGS = frozenset({"SLIPPAGE_CAP", "DEPTH_THIN"})
RISK_REASONS = frozenset(
    {
        "blocked_tag",
        "max_open_mints",
        "reject_cooldown",
        "cooldown",
        "live_disabled",
        "day_loss_breaker",
        "trading_halted",
        "reduce_only",
        "position_cap",
    }
)
RISK_TAGS = frozenset(
    {
        "DAY_LOSS_BREAKER",
        "TRADING_HALTED",
        "COOLDOWN",
        "POSITION_CAP",
        "LIVE_DISABLED",
        "REDUCE_ONLY",
        "MAX_NOTIONAL",
    }
)
# Liquidity listed next to DEPTH_THIN; DEPTH_THIN is impact. Spread stays with liquidity/impact.
IMPACT_TAGS_EXTRA = frozenset({"SPREAD_TOO_WIDE"})
IGNORE_REASONS = frozenset({"auto_paper_orders=false", "hold"})
IGNORE_TAGS = frozenset({"AUTOPAPER_OFF"})

REJECT_BUCKETS = ("progress", "impact", "risk")


def classify_reject_bucket(
    reason: str = "", tags: Optional[Sequence[str]] = None
) -> str:
    """Map stable reason/tags → progress | impact | risk | none."""
    tagset = {str(t).upper() for t in (tags or []) if t}
    if tagset & IGNORE_TAGS:
        return "none"
    raw = (reason or "").strip()
    lowered = raw.lower()
    if lowered in IGNORE_REASONS:
        return "none"
    if lowered in PROGRESS_REASONS:
        return "progress"
    if lowered in IMPACT_REASONS or (tagset & IMPACT_TAGS) or (tagset & IMPACT_TAGS_EXTRA):
        return "impact"
    if lowered in RISK_REASONS or (tagset & RISK_TAGS):
        return "risk"
    # uppercase tag used as reason (RiskGate deny)
    if raw in IMPACT_TAGS or raw in IMPACT_TAGS_EXTRA:
        return "impact"
    if raw in RISK_TAGS:
        return "risk"
    return "none"


def shadow_from_fill(fill: Fill, impact_bps_est: Optional[float]) -> Optional[float]:
    """Paper Fill.slippage vs estimated impact (DecisionLog spec)."""
    slip = fill.slippage_bps
    if slip is None or impact_bps_est is None:
        return fill.shadow_slippage_bps
    return abs(float(slip) - float(impact_bps_est))


def pick_shadow_fill_px(
    fill_ts: int,
    trades: Optional[Sequence[Mapping[str, Any]]] = None,
    candles: Optional[Sequence[Any]] = None,
) -> tuple[Optional[float], Optional[str]]:
    """P0 replay: next tape trade after fill_ts, else next bar open."""
    later: list[tuple[int, float]] = []
    for row in trades or []:
        try:
            ts = int(row.get("ts") or 0)
            px = float(row.get("price") or 0)
        except (TypeError, ValueError, AttributeError):
            continue
        if ts > int(fill_ts) and px > 0:
            later.append((ts, px))
    if later:
        later.sort(key=lambda x: x[0])
        return later[0][1], "next_trade"
    opens: list[tuple[int, float]] = []
    for c in candles or []:
        if isinstance(c, Mapping):
            ts = int(c.get("t") or 0)
            o = c.get("o")
        else:
            ts = int(getattr(c, "t", 0) or 0)
            o = getattr(c, "o", None)
        try:
            fo = float(o) if o is not None else 0.0
        except (TypeError, ValueError):
            continue
        if ts > int(fill_ts) and fo > 0:
            opens.append((ts, fo))
    if opens:
        opens.sort(key=lambda x: x[0])
        return opens[0][1], "next_open"
    return None, None


def signed_shadow_slippage_bps(
    side: Optional[str],
    decision_px: Optional[float],
    shadow_fill_px: Optional[float],
) -> Optional[float]:
    """sign(side) * (shadow − decision) / decision * 1e4. Sell/short flips sign."""
    if decision_px is None or shadow_fill_px is None:
        return None
    try:
        d = float(decision_px)
        s = float(shadow_fill_px)
    except (TypeError, ValueError):
        return None
    if d <= 0 or s <= 0:
        return None
    sign = -1.0 if str(side or "").lower() in {"sell", "short"} else 1.0
    return sign * (s - d) / d * 1e4


def shadow_metrics(
    fill: Fill,
    impact_bps_est: Optional[float],
    *,
    shadow_fill_px: Optional[float] = None,
    source: Optional[str] = None,
    decision_px: Optional[float] = None,
    side: Optional[str] = None,
) -> dict[str, Any]:
    """Replay shadow vs decision_px; impact_error = shadow − estimated."""
    est = impact_bps_est if impact_bps_est is not None else fill.estimated_impact_bps
    src = source
    px = shadow_fill_px
    arrival = decision_px
    if arrival is None and fill.quote_price:
        arrival = float(fill.quote_price)
    if arrival is None and fill.price:
        arrival = float(fill.price)
    paper_px = float(fill.price) if fill.price else None
    slip = signed_shadow_slippage_bps(side, arrival, px)
    if slip is not None:
        src = src or "next_trade"
    else:
        slip = fill.shadow_slippage_bps
        if slip is None:
            slip = shadow_from_fill(fill, est)
        if px is None and fill.quote_price:
            px = float(fill.quote_price)
            src = src or "fill_quote"
            slip = signed_shadow_slippage_bps(side, arrival, px) if arrival else slip
    err = None
    if slip is not None and est is not None:
        err = float(slip) - float(est)
    return {
        "estimated_impact_bps": est,
        "decision_px": arrival,
        "arrival_px": arrival,
        "fill_px": paper_px,
        "paper_fill_px": paper_px,
        "shadow_fill_px": px,
        "shadow_slippage_bps": slip,
        "impact_error_bps": err,
        "shadow_source": src,
    }


def shadow_fields_for_fill(
    fill: Fill,
    symbol: str,
    impact_bps_est: Optional[float],
    *,
    decision_px: Optional[float] = None,
    side: Optional[str] = None,
) -> dict[str, Any]:
    trades: Sequence[Mapping[str, Any]] = []
    candles: Sequence[Any] = []
    try:
        from app.providers import get_provider

        prov = get_provider()
        if hasattr(prov, "get_recent_trades"):
            trades = prov.get_recent_trades(symbol) or []
        if hasattr(prov, "get_candles"):
            candles = prov.get_candles(symbol, "1m") or []
    except Exception:
        trades, candles = [], []
    px, src = pick_shadow_fill_px(fill.ts, trades, candles)
    out = shadow_metrics(
        fill,
        impact_bps_est,
        shadow_fill_px=px,
        source=src,
        decision_px=decision_px,
        side=side,
    )
    out["impact_bps_est"] = impact_bps_est if impact_bps_est is not None else out.get("estimated_impact_bps")
    return out


def backfill_decision_shadows(rows: Sequence[DecisionLogRow]) -> None:
    """Fill in next_trade|next_open when the tape has moved since the paper fill."""
    cache_tr: dict[str, list] = {}
    cache_c: dict[str, list] = {}
    prov = None
    try:
        from app.providers import get_provider

        prov = get_provider()
    except Exception:
        return
    for row in rows:
        if row.outcome not in {"fill", "partial"}:
            continue
        prefer = row.shadow_source in {"next_trade", "next_open"}
        if prefer and row.shadow_fill_px is not None and row.impact_error_bps is not None:
            continue
        if row.symbol not in cache_tr and hasattr(prov, "get_recent_trades"):
            cache_tr[row.symbol] = list(prov.get_recent_trades(row.symbol) or [])
        if row.symbol not in cache_c and hasattr(prov, "get_candles"):
            cache_c[row.symbol] = list(prov.get_candles(row.symbol, "1m") or [])
        px, src = pick_shadow_fill_px(
            row.ts, cache_tr.get(row.symbol) or [], cache_c.get(row.symbol) or []
        )
        if px is None:
            continue
        row.shadow_fill_px = px
        row.shadow_source = src
        decision = row.decision_px or row.arrival_px or row.fill_px or row.paper_fill_px
        slip = signed_shadow_slippage_bps(row.signal_side, decision, px)
        if slip is None:
            continue
        row.shadow_slippage_bps = slip
        est = row.impact_bps_est if row.impact_bps_est is not None else row.estimated_impact_bps
        if est is not None:
            row.impact_error_bps = float(slip) - float(est)


def make_row(
    *,
    ts: int,
    strategy_id: str,
    symbol: str,
    stage: str,
    outcome: str,
    mint: Optional[str] = None,
    signal: Optional[SignalOut] = None,
    risk: Optional[RiskOut] = None,
    signal_side: Optional[str] = None,
    signal_reason: Optional[str] = None,
    signal_tags: Optional[Sequence[str]] = None,
    risk_allow: Optional[bool] = None,
    risk_tags: Optional[Sequence[str]] = None,
    risk_notes: Optional[str] = None,
    notional_sol: Optional[float] = None,
    impact_bps_est: Optional[float] = None,
    impact_bps_cap: Optional[float] = None,
    estimated_impact_bps: Optional[float] = None,
    impact_gross_bps: Optional[float] = None,
    protocol_fee_bps: Optional[float] = None,
    impact_net_bps: Optional[float] = None,
    phase: Optional[str] = None,
    impact_fee_bps: Optional[float] = None,
    pump: Optional[object] = None,
    liquidity: Optional[object] = None,
    decision_px: Optional[float] = None,
    arrival_px: Optional[float] = None,
    fill_px: Optional[float] = None,
    paper_fill_px: Optional[float] = None,
    shadow_fill_px: Optional[float] = None,
    shadow_slippage_bps: Optional[float] = None,
    impact_error_bps: Optional[float] = None,
    shadow_source: Optional[str] = None,
    reject_bucket: Optional[str] = None,
) -> DecisionLogRow:
    side = signal_side
    reason = signal_reason
    stags = list(signal_tags or [])
    if signal is not None:
        side = signal.side
        reason = signal.reason
        stags = list(signal.tags or [])
    r_allow = risk_allow
    r_tags = list(risk_tags or [])
    r_notes = risk_notes
    if risk is not None:
        r_allow = risk.allow
        r_tags = list(risk.tags or [])
        r_notes = risk.notes
    bucket = reject_bucket
    if bucket is None:
        if outcome == "reject":
            bucket = classify_reject_bucket(reason or "", list(stags) + list(r_tags))
        else:
            bucket = "none"
    if bucket not in {"progress", "impact", "risk", "none"}:
        bucket = "none"
    est = estimated_impact_bps if estimated_impact_bps is not None else impact_bps_est
    paper_px = paper_fill_px if paper_fill_px is not None else fill_px
    arrive = arrival_px if arrival_px is not None else decision_px
    venue = impact_venue_phase(pump, phase)
    gross = impact_gross_bps if impact_gross_bps is not None else est
    addon = impact_fee_bps
    if addon is None and liquidity is not None and getattr(liquidity, "fee_bps", None) is not None:
        addon = float(getattr(liquidity, "fee_bps"))
    if addon is None and pump is not None and getattr(pump, "fee_bps", None) is not None:
        addon = float(getattr(pump, "fee_bps"))
    fee = protocol_fee_bps
    if fee is None:
        fee = protocol_fee_bps_for_phase(venue, impact_fee_bps=addon)
    net = compute_impact_net_bps(gross, fee) if gross is not None else None
    stored_est = est if est is not None else gross
    return DecisionLogRow(
        ts=int(ts),
        strategy_id=strategy_id,
        symbol=symbol,
        mint=mint,
        stage=stage,  # type: ignore[arg-type]
        signal_side=side,
        signal_reason=reason,
        signal_tags=stags,
        risk_allow=r_allow,
        risk_tags=r_tags,
        risk_notes=r_notes,
        notional_sol=notional_sol,
        impact_bps_est=impact_bps_est if impact_bps_est is not None else stored_est,
        impact_bps_cap=impact_bps_cap,
        estimated_impact_bps=stored_est,
        impact_gross_bps=gross,
        protocol_fee_bps=float(fee),
        impact_net_bps=net,
        phase=venue,
        decision_px=decision_px if decision_px is not None else arrive,
        arrival_px=arrive,
        fill_px=paper_px,
        paper_fill_px=paper_px,
        shadow_fill_px=shadow_fill_px,
        shadow_slippage_bps=shadow_slippage_bps,
        impact_error_bps=impact_error_bps,
        shadow_source=shadow_source,
        outcome=outcome,  # type: ignore[arg-type]
        reject_bucket=bucket,  # type: ignore[arg-type]
    )


class DecisionLog:
    def __init__(self) -> None:
        self.rows: list[DecisionLogRow] = []
        self._debounce: dict[str, tuple] = {}

    def reset(self) -> None:
        self.rows.clear()
        self._debounce.clear()

    def append(self, row: DecisionLogRow, *, debounce: bool = False) -> Optional[DecisionLogRow]:
        if debounce:
            key = (
                row.stage,
                row.outcome,
                row.signal_reason,
                row.reject_bucket,
                tuple(row.risk_tags),
            )
            if self._debounce.get(row.symbol) == key:
                return None
            self._debounce[row.symbol] = key
        self.rows.append(row)
        if len(self.rows) > LOG_CAP:
            del self.rows[: len(self.rows) - LOG_CAP + 200]
        return row

    def query(
        self,
        *,
        from_ts: Optional[int] = None,
        to_ts: Optional[int] = None,
        limit: int = 200,
    ) -> list[DecisionLogRow]:
        rows = list(self.rows)
        if from_ts is not None:
            rows = [r for r in rows if r.ts >= from_ts]
        if to_ts is not None:
            rows = [r for r in rows if r.ts <= to_ts]
        if limit and limit > 0:
            rows = rows[-limit:]
        return rows

    def all(self) -> list[DecisionLogRow]:
        return list(self.rows)


_log: Optional[DecisionLog] = None


def get_decision_log() -> DecisionLog:
    global _log
    if _log is None:
        _log = DecisionLog()
    return _log


def reset_decision_log() -> None:
    global _log
    _log = None


def append_decision(row: DecisionLogRow, *, debounce: bool = False) -> Optional[DecisionLogRow]:
    return get_decision_log().append(row, debounce=debounce)


def as_dicts(rows: Iterable[DecisionLogRow]) -> list[dict[str, Any]]:
    return [r.model_dump() for r in rows]
