"""Pump.fun paper strategy v1 — SignalOut from curve snapshots + tape (paper only).

See docs/strategies/pump-paper-v1.md. Default auto_paper_orders=false.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from pydantic import BaseModel, field_validator, model_validator

from app.models.contracts import (
    AccountCtx,
    Fill,
    LiquidityCtx,
    OrderIntent,
    PumpfunPaperSnapshot,
    SignalEvent,
    SignalOut,
    SizeIn,
    StrategyContext,
    TickCtx,
)
from app.paper.pipeline import run_paper_order, run_pre_order
from app.paper.guard import live_execution_blocked
from app.paper.decision_log import append_decision, classify_reject_bucket, make_row
from app.providers import get_provider
from app.risk import get_risk_gate

log = logging.getLogger("auu.pump_paper_v1")

STRATEGY_ID = "pump-paper-v1"
FORBIDDEN_TAGS = {"HONEYPOT", "HONEYPOT_FLAG", "TAX_HIGH", "SPREAD_TOO_WIDE"}
EXIT_IMPACT_BPS = 250.0
SELL_PRESSURE_SEC = 30.0
REJECT_STREAK_MAX = 5
REJECT_COOLDOWN_SEC = 600
# Dust floor for the halving walk. Paper clips are smaller than this used to be
# (0.02) so a 0.01 SOL default can still step down on a thin curve.
MIN_NOTIONAL_SOL = 0.001
TAPE_WINDOW_MS = 60_000
# Hard reject. Distill and executability share this ceiling; do not raise it.
IMPACT_HARD_CAP_BPS = 80.0
# Sizing budget, strictly under the executability median gate (60).
# fit_notional stops here instead of climbing up to the 80 bps hard cap.
PAPER_ENTRY_IMPACT_BUDGET_BPS = 55.0
# Library curve quotes still default to fee_bps=125 (add-on 62.5), which floors
# every print above the <60 median gate. Paper entries quote the protocol fee
# (100): one-sided add-on = 50 bps, so a small notional can land under 60.
PAPER_IMPACT_FEE_BPS = 100.0
# Absolute paper clip. Equity percent (0.5%) still applies; this cap binds first
# at the default 10_000 equity (raw 50 SOL → 0.01).
PAPER_MAX_NOTIONAL_SOL = 0.01


class PumpPaperParams(BaseModel):
    """Configurable table from docs/strategies/pump-paper-v1.md §7."""

    progress_bps_min: int = 800
    progress_bps_max: int = 7500
    max_impact_bps: float = IMPACT_HARD_CAP_BPS
    take_profit_pct: float = 0.25
    stop_loss_pct: float = 0.12
    max_hold_sec: int = 900
    cooldown_sec: int = 120
    max_day_loss_pct: float = 0.05
    max_open_mints: int = 3
    notional_pct_equity: float = 0.005
    auto_paper_orders: bool = False
    max_notional_sol: float = PAPER_MAX_NOTIONAL_SOL
    # Paper-only. Hard cap stays max_impact_bps (80). Budget cannot exceed it.
    entry_impact_budget_bps: float = PAPER_ENTRY_IMPACT_BUDGET_BPS
    impact_fee_bps: float = PAPER_IMPACT_FEE_BPS

    @field_validator("max_impact_bps")
    @classmethod
    def _hard_impact_cap(cls, v: float) -> float:
        return min(float(v), IMPACT_HARD_CAP_BPS)

    @field_validator("impact_fee_bps", "max_notional_sol", "notional_pct_equity", "entry_impact_budget_bps")
    @classmethod
    def _non_negative(cls, v: float) -> float:
        return max(0.0, float(v))

    @model_validator(mode="after")
    def _budget_under_hard_cap(self) -> "PumpPaperParams":
        hard = min(IMPACT_HARD_CAP_BPS, float(self.max_impact_bps))
        if float(self.entry_impact_budget_bps) > hard:
            self.entry_impact_budget_bps = hard
        return self


@dataclass
class TapeWindow:
    buy_notional_1m: float = 0.0
    sell_notional_1m: float = 0.0
    trade_count_1m: int = 0
    unique_buyers_5m: Optional[int] = None


@dataclass
class PositionState:
    mint: str
    symbol: str
    qty: float
    entry_price: float
    entry_ts: int
    entry_notional: float


def aggregate_tape(
    trades: list[dict[str, Any]],
    now_ms: int,
    window_ms: int = TAPE_WINDOW_MS,
) -> TapeWindow:
    buy = 0.0
    sell = 0.0
    count = 0
    buyers_5m: set[str] = set()
    five_m = 5 * 60_000
    for row in trades:
        ts = int(row.get("ts") or 0)
        age = now_ms - ts
        side = row.get("side")
        amt = float(row.get("sol_amount") or 0.0)
        if amt <= 0:
            amt = abs(float(row.get("price") or 0.0) * float(row.get("qty") or 0.0))
        if 0 <= age <= five_m and side == "buy":
            key = str(row.get("signature") or row.get("buyer") or "")
            if key:
                buyers_5m.add(key)
        if age > window_ms or age < 0:
            continue
        count += 1
        if side == "buy":
            buy += amt
        elif side == "sell":
            sell += amt
    return TapeWindow(
        buy_notional_1m=buy,
        sell_notional_1m=sell,
        trade_count_1m=count,
        unique_buyers_5m=len(buyers_5m) if buyers_5m else None,
    )


def curve_impact_bps(
    snap: PumpfunPaperSnapshot,
    notional: float,
    side: str,
    fee_bps: Optional[float] = None,
) -> float:
    """Curve impact for a paper quote.

    ``fee_bps=None`` keeps the library default (125). Pump-paper-v1 passes
    ``params.impact_fee_bps`` so entry prints use the paper fee, not a second model.
    """
    liq = LiquidityCtx(
        virtual_sol_reserves=snap.virtual_sol_reserves,
        virtual_token_reserves=snap.virtual_token_reserves,
        real_sol_reserves=snap.real_sol_reserves,
        real_token_reserves=snap.real_token_reserves,
        creator_fee_bps=snap.creator_fee_bps,
        fee_bps=fee_bps,
    )
    return liq.estimated_impact_bps(abs(notional), side=side, pump=snap.to_pump_ctx())


def target_notional_sol(equity: float, params: PumpPaperParams) -> float:
    raw = max(float(equity), 0.0) * float(params.notional_pct_equity)
    return min(raw, float(params.max_notional_sol))


def entry_impact_budget(params: PumpPaperParams) -> float:
    """Paper sizing ceiling: min(hard cap 80, max_impact_bps, entry budget)."""
    hard = min(IMPACT_HARD_CAP_BPS, float(params.max_impact_bps))
    return min(hard, float(params.entry_impact_budget_bps))


def fit_notional(
    snap: PumpfunPaperSnapshot, equity: float, params: PumpPaperParams, side: str = "buy"
) -> Optional[float]:
    """Largest clip at or under target whose curve impact is within the paper budget.

    Does not invent a size. Returns None when even the dust floor is over the
    budget (caller must not fall back to a larger notional).
    """
    limit = entry_impact_budget(params)
    fee = float(params.impact_fee_bps)
    n = target_notional_sol(equity, params)
    while n + 1e-12 >= MIN_NOTIONAL_SOL:
        try:
            impact = curve_impact_bps(snap, n, side, fee_bps=fee)
        except ValueError:
            return None
        if impact <= limit:
            return n
        n *= 0.5
    return None


def evaluate(
    *,
    snapshot: PumpfunPaperSnapshot,
    tape: TapeWindow,
    params: PumpPaperParams,
    now_ms: int,
    impact_entry_bps: float,
    impact_exit_bps: float = 0.0,
    position: Optional[PositionState] = None,
    last_open_ts: Optional[int] = None,
    open_mint_count: int = 0,
    sell_pressure_ms: int = 0,
    extra_tags: Optional[list[str]] = None,
    reject_cooldown: bool = False,
) -> SignalOut:
    """Pure rules from pump-paper-v1.md. Does not submit orders."""
    tags = list(extra_tags or [])
    blocked = [t for t in tags if t in FORBIDDEN_TAGS]

    if position is not None and abs(position.qty) > 0:
        px = max(float(snapshot.price_sol), 1e-18)
        entry = max(float(position.entry_price), 1e-18)
        pnl_pct = (px - entry) / entry if position.qty > 0 else (entry - px) / entry
        hold_sec = (now_ms - position.entry_ts) / 1000.0

        if pnl_pct >= params.take_profit_pct:
            return SignalOut(side="flat", strength=0.9, reason="take_profit", tags=["TAKE_PROFIT"])
        if pnl_pct <= -params.stop_loss_pct:
            return SignalOut(side="flat", strength=0.9, reason="stop_loss", tags=["STOP_LOSS"])
        if snapshot.progress_bps >= 9000 or snapshot.complete or snapshot.migrated:
            return SignalOut(
                side="flat",
                strength=0.85,
                reason="graduation",
                tags=["CURVE_NEAR_GRADUATION"],
            )
        if (
            tape.sell_notional_1m >= 2.0 * max(tape.buy_notional_1m, 1e-18)
            and sell_pressure_ms >= int(SELL_PRESSURE_SEC * 1000)
        ):
            return SignalOut(side="flat", strength=0.8, reason="sell_pressure", tags=["SELL_PRESSURE"])
        if impact_exit_bps > EXIT_IMPACT_BPS:
            return SignalOut(side="flat", strength=0.7, reason="impact_split", tags=["SPLIT_REDUCE"])
        if hold_sec >= params.max_hold_sec:
            return SignalOut(side="flat", strength=0.7, reason="max_hold", tags=["MAX_HOLD"])
        return SignalOut(side="long", strength=0.5, reason="hold", tags=["HOLD"])

    # --- entry ---
    if snapshot.complete or snapshot.migrated:
        return SignalOut(side="flat", strength=0.0, reason="not_curve")
    if not (params.progress_bps_min <= snapshot.progress_bps <= params.progress_bps_max):
        return SignalOut(side="flat", strength=0.0, reason="progress_band")
    if tape.buy_notional_1m < 2.0 * tape.sell_notional_1m or tape.trade_count_1m < 8:
        return SignalOut(side="flat", strength=0.0, reason="momentum")
    if impact_entry_bps > params.max_impact_bps:
        return SignalOut(side="flat", strength=0.0, reason="impact", tags=["SLIPPAGE_CAP"])
    if blocked:
        return SignalOut(side="flat", strength=0.0, reason="blocked_tag", tags=blocked)
    if reject_cooldown:
        return SignalOut(side="flat", strength=0.0, reason="reject_cooldown", tags=["COOLDOWN"])
    if last_open_ts is not None and (now_ms - last_open_ts) < int(params.cooldown_sec * 1000):
        return SignalOut(side="flat", strength=0.0, reason="cooldown", tags=["COOLDOWN"])
    if open_mint_count >= params.max_open_mints:
        return SignalOut(side="flat", strength=0.0, reason="max_open_mints", tags=["POSITION_CAP"])

    strength = min(0.95, 0.55 + min(tape.buy_notional_1m / max(tape.sell_notional_1m, 1e-9) / 10.0, 0.3))
    return SignalOut(
        side="long",
        strength=strength,
        reason="pump_paper_v1_entry",
        tags=["pump-paper-v1"],
    )


@dataclass
class AutoDecision:
    ts: int
    symbol: str
    action: str  # skip | submit | deny | fill | refuse
    allow: bool
    reason: str
    tags: list[str] = field(default_factory=list)
    notes: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "ts": self.ts,
            "symbol": self.symbol,
            "action": self.action,
            "allow": self.allow,
            "reason": self.reason,
            "tags": self.tags,
            "notes": self.notes,
        }


class PumpPaperEngine:
    def __init__(self, params: Optional[PumpPaperParams] = None, equity: float = 10_000.0):
        env_auto = os.getenv("AUTO_PAPER_ORDERS", "").strip().lower()
        base = params or PumpPaperParams()
        if env_auto in {"1", "true", "yes", "on"}:
            base = base.model_copy(update={"auto_paper_orders": True})
        elif env_auto in {"0", "false", "no", "off"}:
            base = base.model_copy(update={"auto_paper_orders": False})
        self.params = base
        self.equity = equity
        self.positions: dict[str, PositionState] = {}
        self._last_open_ts: dict[str, int] = {}
        self._last_emitted: dict[str, SignalOut] = {}
        self._history: list[SignalEvent] = []
        self._sell_pressure_since: dict[str, int] = {}
        self._reject_streak = 0
        self._reject_cool_until = 0
        self._running = False
        self._decisions: list[AutoDecision] = []
        self._last_decision_key: dict[str, tuple] = {}
        self._eval_total = 0
        self._eval_by_bucket: dict[str, int] = {}
        self._eval_by_reason: dict[str, int] = {}
        self._sync_risk_limits()

    def _sync_risk_limits(self) -> None:
        gate = get_risk_gate()
        gate.limits.max_day_loss_pct = float(self.params.max_day_loss_pct)

    def update_params(self, patch: dict[str, Any]) -> PumpPaperParams:
        allowed = set(PumpPaperParams.model_fields)
        clean = {k: v for k, v in patch.items() if k in allowed}
        merged = self.params.model_dump()
        merged.update(clean)
        # Re-validate so max_impact_bps cannot rise above 80 and the budget stays under it.
        self.params = PumpPaperParams.model_validate(merged)
        self._sync_risk_limits()
        return self.params

    def history_for(
        self, symbol: str, from_ts: int | None = None, to_ts: int | None = None
    ) -> list[SignalEvent]:
        out = [e for e in self._history if e.symbol == symbol]
        if from_ts is not None:
            out = [e for e in out if e.t >= from_ts]
        if to_ts is not None:
            out = [e for e in out if e.t <= to_ts]
        return out

    def last_signal(self, symbol: str) -> Optional[SignalOut]:
        return self._last_emitted.get(symbol)

    def last_decisions(self, limit: int = 20) -> list[dict[str, Any]]:
        return [d.as_dict() for d in self._decisions[-limit:]]

    def eval_snapshot(self) -> dict[str, Any]:
        return {
            "total": int(self._eval_total),
            "by_bucket": dict(self._eval_by_bucket),
            "by_reason": dict(self._eval_by_reason),
        }

    def reset_eval_counts(self) -> None:
        self._eval_total = 0
        self._eval_by_bucket = {}
        self._eval_by_reason = {}

    def record_entry_eval(self, signal: SignalOut) -> None:
        """Count one no-position evaluate() for reject_rate (autopaper-off skips ignored)."""
        reason = signal.reason or "unknown"
        if reason == "hold":
            return
        self._eval_total += 1
        self._eval_by_reason[reason] = self._eval_by_reason.get(reason, 0) + 1
        if signal.side == "long":
            self._eval_by_bucket["attempt"] = self._eval_by_bucket.get("attempt", 0) + 1
            return
        bucket = classify_reject_bucket(reason, signal.tags)
        if bucket == "none":
            self._eval_by_bucket["other"] = self._eval_by_bucket.get("other", 0) + 1
        else:
            self._eval_by_bucket[bucket] = self._eval_by_bucket.get(bucket, 0) + 1

    def record_order_reject(self, reason: str, tags: Optional[list[str]] = None) -> None:
        bucket = classify_reject_bucket(reason, tags)
        if bucket == "none":
            bucket = "risk"
        self._eval_by_bucket[bucket] = self._eval_by_bucket.get(bucket, 0) + 1
        key = reason or "order_reject"
        self._eval_by_reason[key] = self._eval_by_reason.get(key, 0) + 1

    def _note(
        self,
        symbol: str,
        now_ms: int,
        *,
        action: str,
        allow: bool,
        reason: str,
        tags: Optional[list[str]] = None,
        notes: str = "",
    ) -> None:
        rec = AutoDecision(
            ts=now_ms,
            symbol=symbol,
            action=action,
            allow=allow,
            reason=reason,
            tags=list(tags or []),
            notes=notes,
        )
        key = (action, reason, tuple(rec.tags), notes)
        debounce = action in {"skip", "refuse"} or (
            action == "deny" and reason in {"TRADING_HALTED", "REDUCE_ONLY"}
        )
        if debounce and self._last_decision_key.get(symbol) == key:
            return
        self._last_decision_key[symbol] = key
        self._decisions.append(rec)
        if len(self._decisions) > 200:
            del self._decisions[: len(self._decisions) - 150]
        level = logging.INFO if action in {"deny", "fill", "refuse", "submit"} else logging.DEBUG
        log.log(
            level,
            "autopaper %s %s allow=%s reason=%s tags=%s notes=%s",
            action,
            symbol,
            allow,
            reason,
            rec.tags,
            notes,
        )

    async def publish_signal(self, symbol: str, now_ms: int, signal: SignalOut) -> SignalEvent:
        ev = SignalEvent(strategyId=STRATEGY_ID, symbol=symbol, t=now_ms, signal=signal)
        self._history.append(ev)
        if len(self._history) > 400:
            del self._history[: len(self._history) - 300]
        from app.bus import get_hub

        await get_hub().publish({"type": "signal", "payload": ev.model_dump()})
        return ev

    def _should_emit(self, symbol: str, signal: SignalOut) -> bool:
        prev = self._last_emitted.get(symbol)
        if signal.side == "long" and signal.reason == "hold":
            return False
        if signal.side == "flat" and (prev is None or prev.side == "flat"):
            return False
        if prev is None:
            return signal.side != "flat"
        return prev.side != signal.side or (
            signal.side != "flat" and prev.reason != signal.reason
        )

    def _apply_fills(self, symbol: str, mint: str, fills: list[Fill], now_ms: int, px: float) -> None:
        pos = self.positions.get(symbol)
        qty = pos.qty if pos else 0.0
        entry = pos.entry_price if pos else 0.0
        entry_ts = pos.entry_ts if pos else now_ms
        for f in fills:
            q = float(f.qty)
            if q > 0:
                new_qty = qty + q
                entry = (entry * qty + float(f.price) * q) / new_qty if new_qty else float(f.price)
                qty = new_qty
                if pos is None:
                    entry_ts = now_ms
            else:
                qty += q
        if qty <= 1e-9:
            self.positions.pop(symbol, None)
            return
        self.positions[symbol] = PositionState(
            mint=mint,
            symbol=symbol,
            qty=qty,
            entry_price=entry if entry > 0 else px,
            entry_ts=entry_ts,
            entry_notional=abs(qty * px),
        )

    def _build_ctx(
        self,
        symbol: str,
        snap: PumpfunPaperSnapshot,
        now_ms: int,
        extra_meta: Optional[dict[str, Any]] = None,
    ) -> StrategyContext:
        provider = get_provider()
        book = provider.snapshot_book(symbol) if hasattr(provider, "snapshot_book") else {}
        spread = float(book.get("spread_bps") or 20.0)
        pos = self.positions.get(symbol)
        gate = get_risk_gate()
        fee = float(self.params.impact_fee_bps)
        pump = snap.to_pump_ctx().model_copy(update={"fee_bps": int(fee)})
        return StrategyContext(
            symbol=symbol,
            ts=now_ms,
            account=AccountCtx(equity=self.equity, day_pnl=gate.day_pnl),
            liquidity=LiquidityCtx(
                spread_bps=spread,
                adv_usd=100_000.0,
                virtual_sol_reserves=snap.virtual_sol_reserves,
                virtual_token_reserves=snap.virtual_token_reserves,
                real_sol_reserves=snap.real_sol_reserves,
                real_token_reserves=snap.real_token_reserves,
                creator_fee_bps=snap.creator_fee_bps,
                fee_bps=fee,
            ),
            position=pos.qty if pos else 0.0,
            tick=TickCtx(mid=max(float(snap.price_sol), 1e-18)),
            pump=pump,
            meta=extra_meta or {},
        )

    async def _submit(
        self,
        ctx: StrategyContext,
        signal: SignalOut,
        side: str,
        notional: float,
        tag: str,
    ) -> dict[str, Any]:
        size = SizeIn(
            target_notional=abs(notional),
            max_slippage_bps=max(float(self.params.max_impact_bps), 150.0),
        )
        # RiskGate curve impact: long→buy, short→sell. Flatten uses short/sell.
        risk_signal = signal if side == "buy" else SignalOut(
            side="short",
            strength=signal.strength,
            reason=signal.reason,
            tags=signal.tags,
        )
        risk = await run_pre_order(ctx, risk_signal, size, strategy_id=STRATEGY_ID)
        if not risk.allow:
            self._on_reject()
            return {"fills": [], "reject": {"tags": risk.tags, "notes": risk.notes or ""}}
        clipped = float(risk.clipped_size or abs(notional))
        intent = OrderIntent(
            side=side,  # type: ignore[arg-type]
            order_type="market",
            qty_or_notional=clipped,
            max_slippage_bps=size.max_slippage_bps,
            client_tag=tag,
        )
        data = await run_paper_order(
            ctx, intent, risk, auto_post_fill=True, close_reason=signal.reason or tag
        )
        if data.get("reject"):
            self._on_reject()
        elif data.get("fills"):
            self._reject_streak = 0
        return data

    def _on_reject(self) -> None:
        self._reject_streak += 1
        if self._reject_streak >= REJECT_STREAK_MAX:
            self._reject_cool_until = int(time.time() * 1000) + REJECT_COOLDOWN_SEC * 1000

    async def maybe_execute(
        self,
        symbol: str,
        snap: PumpfunPaperSnapshot,
        signal: SignalOut,
        now_ms: int,
        notional: float,
    ) -> None:
        if not self.params.auto_paper_orders:
            if signal.side == "long" and signal.reason != "hold":
                self._note(
                    symbol,
                    now_ms,
                    action="skip",
                    allow=False,
                    reason="auto_paper_orders=false",
                    tags=["AUTOPAPER_OFF"],
                    notes=signal.reason,
                )
            return
        blocked, why = live_execution_blocked()
        if blocked:
            self._note(symbol, now_ms, action="refuse", allow=False, reason="LIVE_DISABLED", notes=why)
            self.record_order_reject("LIVE_DISABLED", ["LIVE_DISABLED"])
            append_decision(
                make_row(
                    ts=now_ms,
                    strategy_id=STRATEGY_ID,
                    symbol=symbol,
                    mint=snap.mint,
                    stage="live_blocked",
                    outcome="reject",
                    signal_side=signal.side,
                    signal_reason="LIVE_DISABLED",
                    signal_tags=["LIVE_DISABLED"],
                    risk_allow=False,
                    risk_tags=["LIVE_DISABLED"],
                    risk_notes=why,
                    notional_sol=notional,
                    reject_bucket="risk",
                )
            )
            return
        gate = get_risk_gate()
        if gate.trading_state == "halted":
                self._note(
                    symbol,
                    now_ms,
                    action="deny",
                    allow=False,
                    reason="TRADING_HALTED",
                    tags=["TRADING_HALTED"],
                    notes=signal.reason,
                )
                self.record_order_reject("TRADING_HALTED", ["TRADING_HALTED"])
                return
        pos = self.positions.get(symbol)
        if gate.trading_state == "reducing" and signal.side == "long" and pos is None:
                self._note(
                    symbol,
                    now_ms,
                    action="deny",
                    allow=False,
                    reason="REDUCE_ONLY",
                    tags=["REDUCE_ONLY"],
                    notes=signal.reason,
                )
                self.record_order_reject("REDUCE_ONLY", ["REDUCE_ONLY"])
                return
        ctx = self._build_ctx(symbol, snap, now_ms)

        if signal.side == "long" and pos is None and signal.reason != "hold":
            data = await self._submit(
                ctx, signal, "buy", notional, f"paper:{STRATEGY_ID}"
            )
            fills = [Fill(**{k: v for k, v in f.items() if k != "symbol"}) for f in data.get("fills") or []]
            reject = data.get("reject")
            if fills:
                self._last_open_ts[snap.mint] = now_ms
                self._apply_fills(symbol, snap.mint, fills, now_ms, snap.price_sol)
                self._note(
                    symbol,
                    now_ms,
                    action="fill",
                    allow=True,
                    reason=signal.reason,
                    tags=signal.tags,
                    notes=f"buy fills={len(fills)}",
                )
            else:
                tags = (reject or {}).get("tags") or ["RISK_DENIED"]
                notes = (reject or {}).get("notes") or ""
                self._note(
                    symbol,
                    now_ms,
                    action="deny",
                    allow=False,
                    reason=signal.reason,
                    tags=tags,
                    notes=notes,
                )
                self.record_order_reject(signal.reason, tags)
            return

        if signal.side == "flat" and pos is not None:
            close_notional = abs(pos.qty) * max(float(snap.price_sol), 1e-18)
            if close_notional <= 0:
                self.positions.pop(symbol, None)
                return
            slices = 2 if "SPLIT_REDUCE" in signal.tags else 1
            remaining = close_notional
            filled_any = False
            last_tags: list[str] = []
            last_notes = ""
            for i in range(slices):
                chunk = remaining if i == slices - 1 else close_notional / slices
                data = await self._submit(
                    ctx, signal, "sell", chunk, f"paper:{STRATEGY_ID}:flat"
                )
                fills = [Fill(**{k: v for k, v in f.items() if k != "symbol"}) for f in data.get("fills") or []]
                if fills:
                    self._apply_fills(symbol, snap.mint, fills, now_ms, snap.price_sol)
                    remaining -= chunk
                    filled_any = True
                else:
                    reject = data.get("reject") or {}
                    last_tags = reject.get("tags") or []
                    last_notes = reject.get("notes") or ""
                ctx = self._build_ctx(symbol, snap, now_ms)
            self._note(
                symbol,
                now_ms,
                action="fill" if filled_any else "deny",
                allow=filled_any,
                reason=signal.reason,
                tags=signal.tags or last_tags,
                notes=last_notes or f"flat slices={slices}",
            )
            if not filled_any:
                self.record_order_reject(signal.reason, signal.tags or last_tags)

    def _tape_for(self, provider: Any, symbol: str, now_ms: int) -> TapeWindow:
        trades: list[dict[str, Any]] = []
        getter = getattr(provider, "get_recent_trades", None)
        if callable(getter):
            trades = getter(symbol) or []
        return aggregate_tape(trades, now_ms)

    def monitor_rows(self) -> list[dict[str, Any]]:
        provider = get_provider()
        now_ms = int(time.time() * 1000)
        rows: list[dict[str, Any]] = []
        for info in provider.list_symbols():
            snap = provider.get_pumpfun_snapshot(info.symbol)
            tape = self._tape_for(provider, info.symbol, now_ms)
            tags: list[str] = []
            impact: Optional[float] = None
            if snap is not None:
                tags.append(snap.phase)
                if snap.complete:
                    tags.append("complete")
                if snap.migrated:
                    tags.append("migrated")
                flags = {}
                getter = getattr(provider, "watch_flags", None)
                if callable(getter):
                    flags = getter(info.symbol) or {}
                if flags.get("discovered"):
                    tags.append("discovered")
                    src = flags.get("source")
                    if src and src not in tags:
                        tags.append(str(src))
                n = fit_notional(snap, self.equity, self.params, "buy")
                if n is None:
                    n = target_notional_sol(self.equity, self.params)
                try:
                    impact = curve_impact_bps(
                        snap, n, "buy", fee_bps=self.params.impact_fee_bps
                    )
                except ValueError:
                    impact = None
            last = self._last_emitted.get(info.symbol)
            if last and last.side != "flat":
                tags.append(last.side)
            elif last and last.tags:
                tags.extend([t for t in last.tags if t not in tags])
            rows.append(
                {
                    "symbol": info.symbol,
                    "mint": (snap.mint if snap else info.mint) or "",
                    "kind": info.kind,
                    "progress_bps": snap.progress_bps if snap else None,
                    "phase": snap.phase if snap else None,
                    "complete": bool(snap.complete) if snap else False,
                    "migrated": bool(snap.migrated) if snap else False,
                    "price_sol": snap.price_sol if snap else None,
                    "buy_notional_1m": tape.buy_notional_1m,
                    "sell_notional_1m": tape.sell_notional_1m,
                    "trade_count_1m": tape.trade_count_1m,
                    "estimated_impact_bps": impact,
                    "tags": tags,
                    "signal_side": last.side if last else None,
                    "signal_reason": last.reason if last else None,
                }
            )
        return rows

    async def tick(self) -> None:
        provider = get_provider()
        now_ms = int(time.time() * 1000)
        cool = now_ms < self._reject_cool_until
        for info in provider.list_symbols():
            snap = provider.get_pumpfun_snapshot(info.symbol)
            if snap is None:
                continue
            tape = self._tape_for(provider, info.symbol, now_ms)
            if tape.sell_notional_1m >= 2.0 * max(tape.buy_notional_1m, 1e-18):
                self._sell_pressure_since.setdefault(info.symbol, now_ms)
            else:
                self._sell_pressure_since.pop(info.symbol, None)
            pressure_ms = now_ms - self._sell_pressure_since.get(info.symbol, now_ms)
            if info.symbol not in self._sell_pressure_since:
                pressure_ms = 0

            pos = self.positions.get(info.symbol)
            fee = float(self.params.impact_fee_bps)
            budget = entry_impact_budget(self.params)
            fitted = fit_notional(snap, self.equity, self.params, "buy")
            # Do not substitute a larger clip when the budget cannot be met.
            sized = fitted if fitted is not None else target_notional_sol(self.equity, self.params)
            try:
                impact_in = curve_impact_bps(snap, sized, "buy", fee_bps=fee)
            except ValueError:
                impact_in = 1e9
            impact_out = 0.0
            if pos is not None:
                close_n = abs(pos.qty) * max(float(snap.price_sol), 1e-18)
                try:
                    impact_out = curve_impact_bps(snap, close_n, "sell", fee_bps=fee)
                except ValueError:
                    impact_out = 0.0

            # Real curve quote is what we log. Over-budget entry is rejected on the
            # hard gate (no fill) instead of printing a capped impact number.
            gate_impact = impact_in
            over_budget = fitted is None or impact_in > budget
            if pos is None and over_budget:
                gate_impact = max(impact_in, float(self.params.max_impact_bps) + 1.0)

            signal = evaluate(
                snapshot=snap,
                tape=tape,
                params=self.params,
                now_ms=now_ms,
                impact_entry_bps=gate_impact,
                impact_exit_bps=impact_out,
                position=pos,
                last_open_ts=self._last_open_ts.get(snap.mint),
                open_mint_count=len(self.positions),
                sell_pressure_ms=pressure_ms,
                reject_cooldown=cool,
            )
            if pos is None:
                self.record_entry_eval(signal)
                if signal.reason != "hold":
                    append_decision(
                        make_row(
                            ts=now_ms,
                            strategy_id=STRATEGY_ID,
                            symbol=info.symbol,
                            mint=snap.mint,
                            stage="signal",
                            outcome="emit_signal" if signal.side == "long" else "reject",
                            signal=signal,
                            notional_sol=sized,
                            impact_bps_est=None if impact_in >= 1e8 else impact_in,
                            estimated_impact_bps=None if impact_in >= 1e8 else impact_in,
                            impact_bps_cap=float(self.params.max_impact_bps),
                            decision_px=float(snap.price_sol) if snap.price_sol else None,
                            arrival_px=float(snap.price_sol) if snap.price_sol else None,
                        ),
                        debounce=True,
                    )
            if self._should_emit(info.symbol, signal):
                await self.publish_signal(info.symbol, now_ms, signal)
            self._last_emitted[info.symbol] = signal
            try:
                await self.maybe_execute(info.symbol, snap, signal, now_ms, sized)
            except Exception:
                log.exception("pump-paper-v1 execute failed for %s", info.symbol)

    async def run_loop(self) -> None:
        self._running = True
        interval = float(os.getenv("PUMP_PAPER_LOOP_SEC", "1.0") or "1.0")
        while self._running:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("pump-paper-v1 loop tick failed")
            try:
                await asyncio.sleep(max(interval, 0.25))
            except asyncio.CancelledError:
                raise

    def stop(self) -> None:
        self._running = False


_engine: Optional[PumpPaperEngine] = None


def get_engine() -> PumpPaperEngine:
    global _engine
    if _engine is None:
        _engine = PumpPaperEngine()
    return _engine


def reset_engine() -> None:
    global _engine
    _engine = None


def loop_enabled() -> bool:
    flag = os.getenv("PUMP_PAPER_LOOP", "1").strip().lower()
    return flag not in {"0", "false", "off", "no"}
