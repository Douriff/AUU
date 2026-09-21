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
}

TradingState = Literal["active", "reducing", "halted"]


@dataclass
class RiskLimits:
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

        impact = ctx.liquidity.estimated_impact_bps(abs(notional))
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


_gate: Optional[RiskGate] = None


def get_risk_gate() -> RiskGate:
    global _gate
    if _gate is None:
        _gate = RiskGate()
    return _gate
