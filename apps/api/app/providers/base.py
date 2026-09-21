from __future__ import annotations

from abc import ABC, abstractmethod
from typing import AsyncIterator

from app.models.contracts import Candle, Fill, RiskEvent, SignalEvent, SymbolInfo


class MarketDataProvider(ABC):
    """Market data slot. Default venue is pump.fun (paper/mock). Never holds keys."""

    name: str = "base"
    venue: str = "pump.fun"

    @abstractmethod
    def list_symbols(self) -> list[SymbolInfo]:
        ...

    @abstractmethod
    def get_candles(
        self, symbol: str, interval: str, from_ts: int | None = None, to_ts: int | None = None
    ) -> list[Candle]:
        ...

    @abstractmethod
    def get_signals(
        self, symbol: str, from_ts: int | None = None, to_ts: int | None = None
    ) -> list[SignalEvent]:
        ...

    @abstractmethod
    def get_fills(
        self, symbol: str, from_ts: int | None = None, to_ts: int | None = None
    ) -> list[Fill]:
        ...

    @abstractmethod
    async def stream(self, channel: str, symbol: str, interval: str | None = None) -> AsyncIterator[dict]:
        ...

    def snapshot_book(self, symbol: str) -> dict:
        """Synthetic depth around mid. Pump.fun has no CLOB; this is paper-only."""
        return {"symbol": symbol, "bids": [], "asks": [], "mid": 0.0, "spread_bps": 0.0}

    def snapshot_curve(self, symbol: str) -> dict:
        """Bonding-curve snapshot: virtual reserves, progress, graduation/migration."""
        return {
            "symbol": symbol,
            "venue": self.venue,
            "virtual_sol_reserves": None,
            "virtual_token_reserves": None,
            "curve_progress": None,
            "graduated": False,
            "migrated": False,
        }
