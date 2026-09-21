from __future__ import annotations

from abc import ABC, abstractmethod
from typing import AsyncIterator

from app.models.contracts import Candle, Fill, PumpfunPaperSnapshot, RiskEvent, SignalEvent, SymbolInfo


class MarketDataProvider(ABC):
    name: str = "base"

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

    def get_pumpfun_snapshot(self, symbol: str) -> PumpfunPaperSnapshot | None:
        """Optional Pump.fun curve snapshot. Mock returns None."""
        return None

    def get_recent_trades(self, symbol: str) -> list[dict]:
        """Recent tape rows (PumpfunTradeTick-shaped dicts). Mock returns []."""
        return []

    def register_watch_mint(self, mint: str, **kwargs):
        """Optional: discovery registers a mint onto the paper watchlist. Mock no-op."""
        return None

    def watch_flags(self, symbol: str) -> dict:
        return {}

    @abstractmethod
    async def stream(self, channel: str, symbol: str, interval: str | None = None) -> AsyncIterator[dict]:
        ...
