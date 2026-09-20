"""Frozen contract field names — keep in sync with apps/web/src/types/contracts.ts."""
from __future__ import annotations

from typing import Literal, Optional

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


class EnvelopeOk(BaseModel):
    ok: Literal[True] = True
    data: object


class EnvelopeErr(BaseModel):
    ok: Literal[False] = False
    error: dict
