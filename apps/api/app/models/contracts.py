"""Frozen contract field names — keep in sync with apps/web/src/types/contracts.ts."""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


class SymbolInfo(BaseModel):
    symbol: str
    base: str
    quote: str
    kind: str = "meme_mock"


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

    def estimated_impact_bps(self, notional: float) -> float:
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
