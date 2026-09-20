from __future__ import annotations

from abc import ABC, abstractmethod
from typing import AsyncIterator

from app.models.contracts import Candle, Fill, RiskEvent, SignalEvent, SymbolInfo


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

    @abstractmethod
    async def stream(self, channel: str, symbol: str, interval: str | None = None) -> AsyncIterator[dict]:
        ...
