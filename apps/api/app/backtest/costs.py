"""Cost model shared by backtest and paper (plan §1): taker fee + per-coin slippage, per side.

Slippage is an assumption, not measured; run with ``mult=2`` for the sensitivity check.
"""
from __future__ import annotations

from dataclasses import dataclass, field

TAKER_PERP = 0.0005  # Binance/OKX VIP0 USDT-M perp taker
SLIPPAGE = {"BTC": 1e-4, "ETH": 1e-4, "SOL": 2e-4, "XRP": 2e-4, "DOGE": 2e-4, "BNB": 2e-4}
SLIPPAGE_DEFAULT = 3e-4


@dataclass(frozen=True)
class CostModel:
    taker: float = TAKER_PERP
    slippage: dict = field(default_factory=lambda: dict(SLIPPAGE))
    slippage_default: float = SLIPPAGE_DEFAULT
    mult: float = 1.0
    # Funding is charged from the data (longs pay positive rates), not modelled here.

    def per_side(self, coin: str) -> float:
        return (self.taker + self.slippage.get(coin, self.slippage_default)) * self.mult

    def scaled(self, mult: float) -> "CostModel":
        return CostModel(self.taker, dict(self.slippage), self.slippage_default, self.mult * mult)
