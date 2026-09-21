"""Frozen contract field names — keep in sync with apps/web/src/types/contracts.ts."""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


class SymbolInfo(BaseModel):
    symbol: str
    base: str
    quote: str
    kind: str = "meme_mock"
    mint: Optional[str] = None


class Candle(BaseModel):
    symbol: str
    interval: str
    t: int  # unix ms
    o: float
    h: float
    l: float
    c: float
    v: float


class SignalOut(BaseModel):
    side: Literal["long", "short", "flat"]
    strength: float = 0.5
    reason: str = ""
    expire_ts: Optional[int] = None
    tags: list[str] = Field(default_factory=list)


class RiskOut(BaseModel):
    allow: bool
    clipped_size: Optional[float] = None
    tags: list[str] = Field(default_factory=list)
    notes: Optional[str] = None


class Fill(BaseModel):
    ts: int
    price: float
    qty: float
    fee: Optional[float] = None
    slippage_bps: Optional[float] = None
    tag: Optional[str] = None


class SignalEvent(BaseModel):
    strategyId: str
    symbol: str
    t: int
    signal: SignalOut


class RiskEvent(BaseModel):
    strategyId: Optional[str] = None
    symbol: Optional[str] = None
    t: int
    risk: RiskOut


# --- Paper path context / intent (additive; does not rename frozen fields) ---


class AccountCtx(BaseModel):
    equity: float = 10_000.0
    day_pnl: float = 0.0


class LiquidityCtx(BaseModel):
    spread_bps: float = 20.0
    adv_usd: float = 100_000.0
    # Optional Pump.fun curve fields. When virtual reserves are >0 (here or via
    # `pump=`), estimated_impact_bps uses bonding-curve math instead of CEX sqrt.
    virtual_sol_reserves: Optional[str] = None
    virtual_token_reserves: Optional[str] = None
    real_sol_reserves: Optional[str] = None
    real_token_reserves: Optional[str] = None
    fee_bps: Optional[float] = None
    protocol_fee_bps: Optional[int] = None
    creator_fee_bps: Optional[int] = None

    def estimated_impact_bps(
        self,
        notional: float,
        side: Optional[str] = None,
        pump: Optional[Any] = None,
        fee_bps: Optional[float] = None,
    ) -> float:
        src = pump if pump is not None else self
        if _has_curve_reserves(src):
            return _curve_impact_from_source(
                src,
                notional,
                side,
                fee_bps if fee_bps is not None else self.fee_bps,
            )
        adv = max(self.adv_usd, 1.0)
        # conservative square-root impact + half-spread
        return self.spread_bps / 2 + 40.0 * ((abs(notional) / adv) ** 0.6)


class BookLevel(BaseModel):
    price: float
    size: float


class BookCtx(BaseModel):
    bids: list[BookLevel] = Field(default_factory=list)
    asks: list[BookLevel] = Field(default_factory=list)


class TickCtx(BaseModel):
    mid: float


class PumpCtx(BaseModel):
    """Optional StrategyContext.pump — Pump.fun curve snapshot for paper risk/fill."""

    curve_progress_bps: int = 0
    virtual_sol_reserves: str = "0"
    virtual_token_reserves: str = "0"
    real_sol_reserves: str = "0"
    real_token_reserves: str = "0"
    creator_fee_bps: int = 0
    protocol_fee_bps: Optional[int] = None
    fee_bps: Optional[int] = None  # impact fee override; default 125 in curve math
    complete: bool = False
    migrated: bool = False
    amm_pool: Optional[str] = None


class SizeIn(BaseModel):
    """Sizer output consumed by RiskGate — clipped_size echoes target_notional."""

    target_notional: float
    max_slippage_bps: float = 150.0
    urgency: Literal["low", "normal", "high"] = "normal"


class StrategyContext(BaseModel):
    symbol: str
    ts: int
    account: AccountCtx = Field(default_factory=AccountCtx)
    liquidity: LiquidityCtx = Field(default_factory=LiquidityCtx)
    position: float = 0.0  # signed qty
    features: dict[str, Any] = Field(default_factory=dict)
    meta: dict[str, Any] = Field(default_factory=dict)
    book: Optional[BookCtx] = None
    tick: Optional[TickCtx] = None
    pump: Optional[PumpCtx] = None


class PumpfunPaperSnapshot(BaseModel):
    """WS/REST additive payload: Pump.fun curve paper market."""

    mint: str
    symbol: str
    phase: Literal["curve", "graduating", "amm"] = "curve"
    progress_bps: int = 0
    complete: bool = False
    migrated: bool = False
    virtual_sol_reserves: str
    virtual_token_reserves: str
    real_sol_reserves: str
    real_token_reserves: str
    token_total_supply: str
    price_sol: float
    price_sol_str: Optional[str] = None
    market_cap_sol: Optional[float] = None
    creator_fee_bps: int = 0
    pool: Optional[str] = None
    slot: Optional[int] = None
    updated_ts: int
    synthetic: bool = True

    def to_pump_ctx(self) -> "PumpCtx":
        return PumpCtx(
            curve_progress_bps=self.progress_bps,
            virtual_sol_reserves=self.virtual_sol_reserves,
            virtual_token_reserves=self.virtual_token_reserves,
            real_sol_reserves=self.real_sol_reserves,
            real_token_reserves=self.real_token_reserves,
            creator_fee_bps=self.creator_fee_bps,
            complete=self.complete,
            migrated=self.migrated,
            amm_pool=self.pool,
        )


class PumpfunTradeTick(BaseModel):
    mint: str
    symbol: str
    ts: int
    side: Literal["buy", "sell"]
    price: float
    qty: float
    sol_amount: float
    signature: Optional[str] = None
    phase: Literal["curve", "amm"] = "curve"


class NewTokenEvent(BaseModel):
    """WS type=new_token — discovery only; never an order intent."""

    mint: str
    creator: str = ""
    slot: Optional[int] = None
    initial_reserves: dict[str, str] = Field(default_factory=dict)
    ts: int
    source: Literal["pumpportal", "logs"]


def _reserve_int(obj: Any, name: str) -> int:
    val = getattr(obj, name, None)
    if val is None:
        return 0
    try:
        return int(str(val).strip() or "0")
    except (TypeError, ValueError):
        return 0


def _has_curve_reserves(obj: Any) -> bool:
    """True when virtual reserves can drive CP impact (complete/migrated ignored)."""
    return _reserve_int(obj, "virtual_sol_reserves") > 0 and _reserve_int(
        obj, "virtual_token_reserves"
    ) > 0


def _curve_impact_from_source(
    src: Any,
    notional: float,
    side: Optional[str],
    fee_bps: Optional[float],
) -> float:
    # Lazy import: models stay usable without loading the provider package first.
    from app.providers.pumpfun_curve_math import (
        DEFAULT_IMPACT_FEE_BPS,
        estimated_curve_impact_bps,
    )

    if not side:
        raise ValueError("curve impact requires side='buy'|'sell' (do not share one branch)")
    addon = fee_bps
    if addon is None:
        addon = getattr(src, "fee_bps", None)
    if addon is None:
        addon = DEFAULT_IMPACT_FEE_BPS
    proto = getattr(src, "protocol_fee_bps", None)
    creator = int(getattr(src, "creator_fee_bps", 0) or 0)
    return estimated_curve_impact_bps(
        _reserve_int(src, "virtual_sol_reserves"),
        _reserve_int(src, "virtual_token_reserves"),
        _reserve_int(src, "real_sol_reserves"),
        _reserve_int(src, "real_token_reserves"),
        abs(float(notional)),
        side,
        fee_bps=int(addon),
        protocol_fee_bps=int(proto) if proto is not None else None,
        creator_fee_bps=creator,
    )


class OrderIntent(BaseModel):
    side: Literal["buy", "sell"]
    order_type: Literal["market", "limit", "twap_sim"] = "market"
    qty_or_notional: float
    limit_price: Optional[float] = None
    max_slippage_bps: float = 150.0
    client_tag: str = "paper"
    expire_ts: Optional[int] = None


class RejectOut(BaseModel):
    tags: list[str] = Field(default_factory=list)
    notes: str = ""


class EnvelopeOk(BaseModel):
    ok: Literal[True] = True
    data: object


class EnvelopeErr(BaseModel):
    ok: Literal[False] = False
    error: dict
