"""RiskGate — pre_order hard gate + post_fill integrity / day-loss breaker (paper only)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional

from app.models.contracts import Fill, RiskOut, SignalOut, SizeIn, StrategyContext

REASON = {
    "DAY_LOSS_BREAKER",
    "SPREAD_TOO_WIDE",
    "DEPTH_THIN",
    "SLIPPAGE_CAP",
    "MEV_SUSPECT",
    "HONEYPOT_FLAG",
    "TAX_HIGH",
    "POSITION_CAP",
    "COOLDOWN",
    "MAX_NOTIONAL",
    "TRADING_HALTED",
    "REDUCE_ONLY",
    "OVERFILL",
    "DUP_FILL",
    "CURVE_NEAR_GRADUATION",
    # Live extras (fail closed when unset). Paper check() ignores these tags.
    "LIMITS_MISSING",
    "LIVE_DISABLED",
    "NO_KEYPAIR",
    "MAX_OPEN_MINTS",
}

TradingState = Literal["active", "reducing", "halted"]


@dataclass
class RiskLimits:
    """Paper-only limits. Live caps live on app.live.gate.LiveLimits — do not add them here."""

    max_notional_per_symbol: float = 2_000.0
    max_day_loss_pct: float = 0.05
    max_spread_bps: float = 80.0
    cooldown_sec_after_reject: float = 30.0


@dataclass
class RiskGate:
    limits: RiskLimits = field(default_factory=RiskLimits)
    meme: dict = field(
        default_factory=lambda: {
            "target_vol_daily": 0.1,
            "max_single_symbol_pct": 0.1,
            "min_adv_usd": 5e4,
            "impact_cap_bps": 150.0,
            "honeypot_block": True,
            "max_tax_pct": 10.0,
            "mev_guard": True,
            "rug_velocity_guard": True,
        }
    )
    _cooldown_until: dict[str, int] = field(default_factory=dict)
    _seen_fills: set[tuple] = field(default_factory=set)
    _trading_state: TradingState = "active"
    _day_pnl: float = 0.0
    _equity_at_day_start: float = 10_000.0

    @property
    def trading_state(self) -> TradingState:
        return self._trading_state

    @property
    def day_pnl(self) -> float:
        return self._day_pnl

    def check(self, ctx: StrategyContext, signal: SignalOut, size: SizeIn) -> RiskOut:
        tags: list[str] = []
        notes: list[str] = []
        notional = float(size.target_notional)
        max_slip = float(size.max_slippage_bps)
        urgency = size.urgency

        # --- trading state hard gate ---
        if self._trading_state == "halted":
            return RiskOut(allow=False, clipped_size=None, tags=["TRADING_HALTED"], notes="halted")

        if self._trading_state == "reducing":
            if not self._is_reducing(ctx.position, notional, signal.side):
                return RiskOut(
                    allow=False,
                    clipped_size=None,
                    tags=["REDUCE_ONLY"],
                    notes="reducing: open denied",
                )

        # --- post_size clip ---
        max_nom = self.limits.max_notional_per_symbol
        if abs(notional) > max_nom:
            notional = _sign(notional) * max_nom
            tags.append("POSITION_CAP")
            notes.append(f"clip notional to {max_nom}")

        rv = ctx.features.get("realized_vol_daily")
        tv = float(self.meme.get("target_vol_daily", 0.1))
        if rv and float(rv) > 0:
            scale = min(1.0, tv / float(rv))
            if scale < 1.0:
                notional *= scale
                notes.append(f"vol_scale={scale:.3f}")

        eq = max(ctx.account.equity, 1e-9)
        max_pct = float(self.meme.get("max_single_symbol_pct", 0.1))
        if abs(notional) / eq > max_pct:
            cap = eq * max_pct
            notional = _sign(notional) * cap
            tags.append("POSITION_CAP")

        # --- pre_order denies ---
        day_pnl = ctx.account.day_pnl if ctx.account.day_pnl is not None else self._day_pnl
        if day_pnl / eq <= -self.limits.max_day_loss_pct:
            return RiskOut(
                allow=False, clipped_size=None, tags=["DAY_LOSS_BREAKER"], notes="day loss breaker"
            )

        if ctx.liquidity.spread_bps > self.limits.max_spread_bps:
            return RiskOut(allow=False, clipped_size=None, tags=["SPREAD_TOO_WIDE"], notes="spread")

        if ctx.liquidity.adv_usd < float(self.meme.get("min_adv_usd", 5e4)):
            return RiskOut(allow=False, clipped_size=None, tags=["DEPTH_THIN"], notes="adv")

        impact = ctx.liquidity.estimated_impact_bps(
            abs(notional),
            side=_impact_side(signal, notional),
            pump=ctx.pump,
        )
        if ctx.pump is not None:
            near = ctx.pump.curve_progress_bps >= 9500 or ctx.pump.complete or ctx.pump.migrated
            if near:
                impact *= 1.5
                tags.append("CURVE_NEAR_GRADUATION")
                notes.append("curve ≥95% / complete: impact ×1.5")

        if impact > float(self.meme.get("impact_cap_bps", 150)) or impact > max_slip:
            deny_tags = ["SLIPPAGE_CAP"]
            if "CURVE_NEAR_GRADUATION" in tags:
                deny_tags.append("CURVE_NEAR_GRADUATION")
            return RiskOut(
                allow=False,
                clipped_size=None,
                tags=deny_tags,
                notes=f"impact={impact:.1f}",
            )

        if self.meme.get("honeypot_block") and ctx.meta.get("honeypot"):
            return RiskOut(allow=False, clipped_size=None, tags=["HONEYPOT_FLAG"], notes="honeypot")

        tax = ctx.meta.get("tax_pct")
        if tax is not None and float(tax) > float(self.meme.get("max_tax_pct", 10)):
            return RiskOut(allow=False, clipped_size=None, tags=["TAX_HIGH"], notes=f"tax={tax}")

        until = self._cooldown_until.get(ctx.symbol, 0)
        if ctx.ts < until:
            return RiskOut(allow=False, clipped_size=None, tags=["COOLDOWN"], notes="entry cooldown")

        if (
            self.meme.get("mev_guard")
            and urgency == "high"
            and abs(notional) > eq * 0.05
        ):
            urgency = "normal"
            tags.append("MEV_SUSPECT")
            notes.append("downgrade urgency")

        if self.meme.get("rug_velocity_guard") and ctx.meta.get("rug_velocity"):
            if signal.side != "flat" and not self._is_reducing(ctx.position, notional, signal.side):
                return RiskOut(
                    allow=False,
                    clipped_size=None,
                    tags=["DEPTH_THIN"],
                    notes="rug velocity: flat-only",
                )

        return RiskOut(
            allow=True,
            clipped_size=abs(notional),
            tags=tags,
            notes="; ".join(notes) if notes else "ok",
        )

    def check_live(
        self,
        ctx: StrategyContext,
        signal: SignalOut,
        size: SizeIn,
        *,
        live_limits: Optional[dict] = None,
    ) -> RiskOut:
        """Live pre-order extras using LiveLimits only — never paper RiskLimits.

        Mapped from vn.py RiskManager (see docs/adapters/vnpy-riskmanager-v0.md):
        max_notional_sol=1.0, max_day_loss_pct=0.045, max_open_mints=10.
        Paper `check()` is unchanged and is not called here.

        Rejects with LIVE_DISABLED (RiskOut.tags) unless keypair mounted AND
        secondary confirm AND liveEnabled AND limits present.
        """
        from app.live.gate import (
            LOCKED_MAX_DAY_LOSS_PCT,
            LOCKED_MAX_NOTIONAL_SOL,
            LOCKED_MAX_OPEN_MINTS,
            REASON_LIMITS_MISSING,
            REASON_LIVE_DISABLED,
            REASON_NO_KEYPAIR,
            evaluate,
        )

        status = evaluate()
        gate_tags: list[str] = []
        if not status.armed:
            gate_tags.append(REASON_LIVE_DISABLED)
            for t in status.reasons:
                if t not in gate_tags:
                    gate_tags.append(t)
            if not status.keypair_configured and REASON_NO_KEYPAIR not in gate_tags:
                gate_tags.append(REASON_NO_KEYPAIR)

        src = live_limits if live_limits is not None else status.limits.as_dict()
        max_notional_sol = _live_positive_float(src.get("max_notional_sol") if isinstance(src, dict) else None)
        max_day_loss_pct = _live_positive_float(src.get("max_day_loss_pct") if isinstance(src, dict) else None)
        max_open_mints = _live_positive_int(src.get("max_open_mints") if isinstance(src, dict) else None)
        missing: list[str] = []
        if max_notional_sol is None:
            missing.append("max_notional_sol")
        if max_day_loss_pct is None:
            missing.append("max_day_loss_pct")
        if max_open_mints is None:
            missing.append("max_open_mints")
        if missing or REASON_LIMITS_MISSING in status.reasons:
            tags = list(gate_tags)
            if REASON_LIMITS_MISSING not in tags:
                tags.append(REASON_LIMITS_MISSING)
            return RiskOut(
                allow=False,
                clipped_size=None,
                tags=tags or [REASON_LIMITS_MISSING],
                notes="live limits missing or zero: " + ",".join(missing or status.limits_missing),
            )

        if gate_tags:
            return RiskOut(
                allow=False,
                clipped_size=None,
                tags=gate_tags,
                notes="live gate closed: " + ",".join(status.reasons),
            )

        max_notional_sol = min(float(max_notional_sol), LOCKED_MAX_NOTIONAL_SOL)
        max_day_loss_pct = min(float(max_day_loss_pct), LOCKED_MAX_DAY_LOSS_PCT)
        max_open_mints = min(int(max_open_mints), LOCKED_MAX_OPEN_MINTS)

        notional = abs(float(size.target_notional))
        if notional > float(max_notional_sol):
            return RiskOut(
                allow=False,
                clipped_size=None,
                tags=["MAX_NOTIONAL"],
                notes=f"live notional {notional} > max_notional_sol {max_notional_sol}",
            )

        eq = max(ctx.account.equity, 1e-9)
        day_pnl = ctx.account.day_pnl if ctx.account.day_pnl is not None else self._day_pnl
        if day_pnl / eq <= -float(max_day_loss_pct):
            return RiskOut(
                allow=False,
                clipped_size=None,
                tags=["DAY_LOSS_BREAKER"],
                notes="live day-loss circuit breaker",
            )

        open_mints = 0
        if ctx.meta and ctx.meta.get("open_mints") is not None:
            try:
                open_mints = int(ctx.meta.get("open_mints") or 0)
            except (TypeError, ValueError):
                open_mints = 0
        is_open = signal.side != "flat" and not self._is_reducing(ctx.position, float(size.target_notional), signal.side)
        if is_open and open_mints >= int(max_open_mints):
            return RiskOut(
                allow=False,
                clipped_size=None,
                tags=["MAX_OPEN_MINTS"],
                notes=f"live open mints {open_mints} >= max_open_mints {max_open_mints}",
            )

        return RiskOut(
            allow=True,
            clipped_size=abs(notional),
            tags=[],
            notes="ok",
        )

    def on_reject(self, ctx: StrategyContext, tags: list[str]) -> None:
        cd = self.limits.cooldown_sec_after_reject
        self._cooldown_until[ctx.symbol] = ctx.ts + int(cd * 1000)

    def post_fill(self, ctx: StrategyContext, fill: Fill) -> dict:
        """Integrity + day-loss → trading_state. Never fabricates fills."""
        tags: list[str] = []
        notes: list[str] = []
        key = (fill.ts, fill.price, fill.qty, fill.tag)
        if key in self._seen_fills:
            tags.append("DUP_FILL")
            notes.append("duplicate fill ignored")
            return {
                "trading_state": self._trading_state,
                "tags": tags,
                "notes": "; ".join(notes),
            }
        self._seen_fills.add(key)

        # naive overfill: qty magnitude vs position notionally unbounded — flag absurd qty
        if abs(fill.qty) > 1e9:
            tags.append("OVERFILL")
            notes.append("overfill qty")
            return {
                "trading_state": self._trading_state,
                "tags": tags,
                "notes": "; ".join(notes),
            }

        # mark-to-mid PnL approx: fee drag only for v0
        fee = float(fill.fee or 0.0)
        self._day_pnl -= fee
        eq = max(ctx.account.equity, self._equity_at_day_start, 1e-9)
        # fold in account day_pnl if provided
        day = ctx.account.day_pnl if ctx.account.day_pnl else self._day_pnl
        prev = self._trading_state
        if day / eq <= -self.limits.max_day_loss_pct:
            self._trading_state = "halted"
            tags.append("DAY_LOSS_BREAKER")
            notes.append("day loss → halted")
        elif day / eq <= -self.limits.max_day_loss_pct * 0.6:
            self._trading_state = "reducing"
            notes.append("elevated loss → reducing")

        out: dict = {
            "trading_state": self._trading_state,
            "tags": tags,
            "notes": "; ".join(notes) if notes else "ok",
        }
        if prev != self._trading_state:
            out["state_changed"] = True
            out["reason"] = tags[0] if tags else notes[0] if notes else "state"
        return out

    @staticmethod
    def _is_reducing(position: float, notional: float, side: str) -> bool:
        if position == 0:
            return False
        # long position + short/sell reduces; short position + long/buy reduces
        if position > 0 and (side == "short" or notional < 0):
            return True
        if position < 0 and (side == "long" or notional > 0):
            return True
        return False


def _sign(x: float) -> float:
    return 1.0 if x >= 0 else -1.0


def _live_positive_float(value: object) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        n = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    return n


def _live_positive_int(value: object) -> Optional[int]:
    n = _live_positive_float(value)
    if n is None:
        return None
    i = int(n)
    if i <= 0:
        return None
    return i


def _impact_side(signal: SignalOut, notional: float) -> str:
    """Buy vs sell must stay distinct for curve impact (long→buy, short→sell)."""
    if signal.side == "short":
        return "sell"
    if signal.side == "long":
        return "buy"
    return "sell" if notional < 0 else "buy"


_gate: Optional[RiskGate] = None


def get_risk_gate() -> RiskGate:
    global _gate
    if _gate is None:
        _gate = RiskGate()
    return _gate


def reset_risk_gate() -> None:
    global _gate
    _gate = None
