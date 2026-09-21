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

from pydantic import BaseModel, Field

from app.models.contracts import (
    AccountCtx,
    Fill,
    LiquidityCtx,
    OrderIntent,
    PumpfunPaperSnapshot,
    RiskOut,
    SignalEvent,
    SignalOut,
    SizeIn,
    StrategyContext,
    TickCtx,
)
from app.providers.pumpfun_curve_math import (
    INITIAL_REAL_TOKEN_RESERVES,
    INITIAL_VIRTUAL_SOL_RESERVES,
    INITIAL_VIRTUAL_TOKEN_RESERVES,
    TOKEN_TOTAL_SUPPLY,
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
# Gross entry impact hard ceiling. Go uses the same 80. Entries reject above it.
HARD_MAX_ENTRY_IMPACT_BPS = 80.0
# Operating buffer under the hard ceiling. Default max_impact_bps.
ENTRY_IMPACT_BUFFER_BPS = 75.0
SELL_PRESSURE_SEC = 30.0
REJECT_STREAK_MAX = 5
REJECT_COOLDOWN_SEC = 600
MIN_NOTIONAL_SOL = 0.02
TAPE_WINDOW_MS = 60_000


class PumpPaperParams(BaseModel):
    """Configurable table from docs/strategies/pump-paper-v1.md §7."""

    # Paper round 4 Go window (2026-09-21). Process defaults, not a runtime patch.
    progress_bps_min: int = 1200
    progress_bps_max: int = 6500
    # Buffer under HARD_MAX_ENTRY_IMPACT_BPS (80). Cannot be raised past 80.
    max_impact_bps: float = ENTRY_IMPACT_BUFFER_BPS
    # Go-window exits: TP 10% above SL 7%, hold 300s. liveEnabled stays false.
    take_profit_pct: float = 0.10
    stop_loss_pct: float = 0.07
    max_hold_sec: int = 300
    cooldown_sec: int = 120
    max_day_loss_pct: float = 0.05
    max_open_mints: int = 3
    notional_pct_equity: float = 0.005
    auto_paper_orders: bool = False
    # Smaller paper clip keeps curve impact under the 75 bps buffer.
    max_notional_sol: float = 0.12


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


def curve_impact_bps(snap: PumpfunPaperSnapshot, notional: float, side: str) -> float:
    liq = LiquidityCtx(
        virtual_sol_reserves=snap.virtual_sol_reserves,
        virtual_token_reserves=snap.virtual_token_reserves,
        real_sol_reserves=snap.real_sol_reserves,
        real_token_reserves=snap.real_token_reserves,
        creator_fee_bps=snap.creator_fee_bps,
    )
    return liq.estimated_impact_bps(abs(notional), side=side, pump=snap.to_pump_ctx())


def entry_impact_limit_bps(params: PumpPaperParams) -> float:
    """Entry cap: operating buffer, never above the gross hard max of 80."""
    return min(float(params.max_impact_bps), HARD_MAX_ENTRY_IMPACT_BPS)


def target_notional_sol(equity: float, params: PumpPaperParams) -> float:
    raw = max(float(equity), 0.0) * float(params.notional_pct_equity)
    return min(raw, float(params.max_notional_sol))


def fit_notional(
    snap: PumpfunPaperSnapshot, equity: float, params: PumpPaperParams, side: str = "buy"
) -> Optional[float]:
    """Walk notional down until gross impact is within the entry cap (<= 80)."""
    limit = entry_impact_limit_bps(params)
    n = target_notional_sol(equity, params)
    while n >= MIN_NOTIONAL_SOL:
        try:
            impact = curve_impact_bps(snap, n, side)
        except ValueError:
            return None
        if impact <= limit and impact <= HARD_MAX_ENTRY_IMPACT_BPS:
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
    # Hard reject: gross impact above 80 never enters, even if max_impact_bps is higher.
    # Default buffer is 75, so a print between 75 and 80 is also rejected.
    if impact_entry_bps > HARD_MAX_ENTRY_IMPACT_BPS or impact_entry_bps > entry_impact_limit_bps(params):
        tags = ["SLIPPAGE_CAP"]
        if impact_entry_bps > HARD_MAX_ENTRY_IMPACT_BPS:
            tags.append("GROSS_IMPACT_HARD")
        return SignalOut(side="flat", strength=0.0, reason="impact", tags=tags)
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
        # Last live curve per symbol. Used to mark orphan paper exits after the
        # mint drops out of the discovery/watch list.
        self._last_snap: dict[str, PumpfunPaperSnapshot] = {}
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
        if clean.get("max_impact_bps") is not None:
            clean["max_impact_bps"] = min(float(clean["max_impact_bps"]), HARD_MAX_ENTRY_IMPACT_BPS)
        self.params = self.params.model_copy(update=clean)
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
            self._last_snap.pop(symbol, None)
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
            ),
            position=pos.qty if pos else 0.0,
            tick=TickCtx(mid=max(float(snap.price_sol), 1e-18)),
            pump=snap.to_pump_ctx(),
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
        # Entries cannot slip past the gross hard max (80) or the 75 bps buffer.
        # Exits stay on the wider cap so a paper flat is not stuck behind entry impact.
        if side == "buy":
            slip_cap = entry_impact_limit_bps(self.params)
        else:
            slip_cap = max(float(self.params.max_impact_bps), 150.0)
        size = SizeIn(
            target_notional=abs(notional),
            max_slippage_bps=slip_cap,
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
        entry_impact_bps: Optional[float] = None,
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
                    phase=snap.phase,
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
        meta: dict[str, Any] = {"mint": snap.mint, "phase": snap.phase}
        if pos is None and entry_impact_bps is not None and entry_impact_bps < 1e8:
            meta["entry_impact_bps"] = float(entry_impact_bps)
        ctx = self._build_ctx(symbol, snap, now_ms, extra_meta=meta)

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

    def _symbols_for_tick(self, provider: Any) -> list[str]:
        """Watch-list symbols union open paper positions.

        A held mint can leave ``list_symbols`` when discovery drops it. Exits
        still have to run, or ``max_hold`` / take-profit / stop-loss never fire
        and ``max_open_mints`` stays full.
        """
        symbols: list[str] = []
        seen: set[str] = set()
        lister = getattr(provider, "list_symbols", None)
        listed = lister() if callable(lister) else []
        for info in listed or []:
            sym = getattr(info, "symbol", None)
            if sym is None and isinstance(info, dict):
                sym = info.get("symbol")
            if not sym or sym in seen:
                continue
            seen.add(sym)
            symbols.append(str(sym))
        for sym in list(self.positions.keys()):
            if sym not in seen:
                seen.add(sym)
                symbols.append(sym)
        return symbols

    def _mark_snapshot(
        self, provider: Any, symbol: str, now_ms: int
    ) -> tuple[Optional[PumpfunPaperSnapshot], bool]:
        """Return ``(snapshot, orphan)``.

        ``orphan`` is true when an open position has no live provider snapshot.
        """
        getter = getattr(provider, "get_pumpfun_snapshot", None)
        live = getter(symbol) if callable(getter) else None
        if live is not None:
            self._last_snap[symbol] = live
            return live, False
        pos = self.positions.get(symbol)
        if pos is None:
            return None, False
        return self._orphan_snapshot(pos, now_ms), True

    def _orphan_snapshot(self, pos: PositionState, now_ms: int) -> PumpfunPaperSnapshot:
        """Last known curve, or a minimal paper mark at ``entry_price``.

        The synthetic curve uses initial virtual reserves so a paper sell's
        impact stays inside the paper cap. It is not a chain quote.
        """
        cached = self._last_snap.get(pos.symbol)
        if cached is not None:
            return cached
        px = max(float(pos.entry_price), 1e-18)
        return PumpfunPaperSnapshot(
            mint=pos.mint or pos.symbol,
            symbol=pos.symbol,
            phase="curve",
            progress_bps=0,
            complete=False,
            migrated=False,
            virtual_sol_reserves=str(INITIAL_VIRTUAL_SOL_RESERVES),
            virtual_token_reserves=str(INITIAL_VIRTUAL_TOKEN_RESERVES),
            real_sol_reserves="0",
            real_token_reserves=str(INITIAL_REAL_TOKEN_RESERVES),
            token_total_supply=str(TOKEN_TOTAL_SUPPLY),
            price_sol=px,
            creator_fee_bps=0,
            updated_ts=now_ms,
            synthetic=True,
        )

    async def _force_orphan_flat(
        self,
        symbol: str,
        snap: PumpfunPaperSnapshot,
        pos: PositionState,
        now_ms: int,
    ) -> None:
        """Paper sell at the last mark when a normal exit did not close the mint.

        Paper only: no live send. Reason is ``max_hold`` with tag ``ORPHAN_EXIT``.
        Bypasses curve-impact / halt denies that would otherwise leave the
        position stuck after the mint left the watch list.
        """
        px = max(float(snap.price_sol), float(pos.entry_price), 1e-18)
        close_notional = abs(float(pos.qty)) * px
        if close_notional <= 0:
            self.positions.pop(symbol, None)
            self._last_snap.pop(symbol, None)
            return
        ctx = StrategyContext(
            symbol=symbol,
            ts=now_ms,
            account=AccountCtx(equity=self.equity, day_pnl=get_risk_gate().day_pnl),
            liquidity=LiquidityCtx(spread_bps=20.0, adv_usd=100_000.0),
            position=pos.qty,
            tick=TickCtx(mid=px),
            meta={"mint": pos.mint, "orphan_exit": True},
        )
        signal = SignalOut(
            side="flat",
            strength=0.7,
            reason="max_hold",
            tags=["MAX_HOLD", "ORPHAN_EXIT"],
        )
        intent = OrderIntent(
            side="sell",
            order_type="market",
            qty_or_notional=close_notional,
            max_slippage_bps=10_000.0,
            client_tag=f"paper:{STRATEGY_ID}:flat",
        )
        risk = RiskOut(
            allow=True,
            clipped_size=close_notional,
            tags=["ORPHAN_EXIT"],
            notes="orphan_exit",
        )
        data = await run_paper_order(
            ctx, intent, risk, auto_post_fill=True, close_reason="max_hold"
        )
        fills = [
            Fill(**{k: v for k, v in f.items() if k != "symbol"}) for f in data.get("fills") or []
        ]
        if fills:
            self._apply_fills(symbol, snap.mint or pos.mint, fills, now_ms, px)
            self._note(
                symbol,
                now_ms,
                action="fill",
                allow=True,
                reason="max_hold",
                tags=signal.tags,
                notes="orphan_exit",
            )
            return
        reject = data.get("reject") or {}
        self._note(
            symbol,
            now_ms,
            action="deny",
            allow=False,
            reason="max_hold",
            tags=reject.get("tags") or ["ORPHAN_EXIT"],
            notes=reject.get("notes") or "orphan_exit",
        )
        log.warning("orphan max_hold paper sell did not fill for %s", symbol)

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
                n = target_notional_sol(self.equity, self.params)
                try:
                    impact = curve_impact_bps(snap, n, "buy")
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
        for symbol in self._symbols_for_tick(provider):
            snap, orphan = self._mark_snapshot(provider, symbol, now_ms)
            if snap is None:
                continue
            tape = self._tape_for(provider, symbol, now_ms)
            if tape.sell_notional_1m >= 2.0 * max(tape.buy_notional_1m, 1e-18):
                self._sell_pressure_since.setdefault(symbol, now_ms)
            else:
                self._sell_pressure_since.pop(symbol, None)
            pressure_ms = now_ms - self._sell_pressure_since.get(symbol, now_ms)
            if symbol not in self._sell_pressure_since:
                pressure_ms = 0

            pos = self.positions.get(symbol)
            sized = fit_notional(snap, self.equity, self.params, "buy") or target_notional_sol(
                self.equity, self.params
            )
            try:
                impact_in = curve_impact_bps(snap, sized, "buy")
            except ValueError:
                impact_in = 1e9
            impact_out = 0.0
            if pos is not None:
                close_n = abs(pos.qty) * max(float(snap.price_sol), 1e-18)
                try:
                    impact_out = curve_impact_bps(snap, close_n, "sell")
                except ValueError:
                    impact_out = 0.0

            signal = evaluate(
                snapshot=snap,
                tape=tape,
                params=self.params,
                now_ms=now_ms,
                impact_entry_bps=impact_in,
                impact_exit_bps=impact_out,
                position=pos,
                last_open_ts=self._last_open_ts.get(snap.mint),
                open_mint_count=len(self.positions),
                sell_pressure_ms=pressure_ms,
                reject_cooldown=cool,
            )
            if orphan and signal.side == "flat" and "ORPHAN_EXIT" not in signal.tags:
                signal = signal.model_copy(update={"tags": [*signal.tags, "ORPHAN_EXIT"]})
            if pos is None:
                self.record_entry_eval(signal)
                if signal.reason != "hold":
                    append_decision(
                        make_row(
                            ts=now_ms,
                            strategy_id=STRATEGY_ID,
                            symbol=symbol,
                            mint=snap.mint,
                            stage="signal",
                            outcome="emit_signal" if signal.side == "long" else "reject",
                            signal=signal,
                            notional_sol=sized,
                            impact_bps_est=None if impact_in >= 1e8 else impact_in,
                            estimated_impact_bps=None if impact_in >= 1e8 else impact_in,
                            impact_bps_cap=float(self.params.max_impact_bps),
                            phase=snap.phase,
                            pump=snap.to_pump_ctx(),
                            decision_px=float(snap.price_sol) if snap.price_sol else None,
                            arrival_px=float(snap.price_sol) if snap.price_sol else None,
                        ),
                        debounce=True,
                    )
            if self._should_emit(symbol, signal):
                await self.publish_signal(symbol, now_ms, signal)
            self._last_emitted[symbol] = signal
            try:
                await self.maybe_execute(
                    symbol, snap, signal, now_ms, sized, entry_impact_bps=impact_in
                )
            except Exception:
                log.exception("pump-paper-v1 execute failed for %s", symbol)
            held = self.positions.get(symbol)
            if (
                orphan
                and self.params.auto_paper_orders
                and held is not None
                and (now_ms - held.entry_ts) / 1000.0 >= float(self.params.max_hold_sec)
            ):
                blocked, _why = live_execution_blocked()
                if not blocked:
                    try:
                        await self._force_orphan_flat(symbol, snap, held, now_ms)
                    except Exception:
                        log.exception("pump-paper-v1 orphan exit failed for %s", symbol)

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
