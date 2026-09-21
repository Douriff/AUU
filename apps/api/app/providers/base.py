from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, AsyncIterator

from app.models.contracts import Candle, Fill, PumpfunPaperSnapshot, RiskEvent, SignalEvent, SymbolInfo


class MarketDataProvider(ABC):
    """Market data slot. pumpfun_paper venue is Pump.fun. Never holds keys."""

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

    @abstractmethod
    async def stream(self, channel: str, symbol: str, interval: str | None = None) -> AsyncIterator[dict]:
        ...

    def snapshot_book(self, symbol: str) -> dict:
        """Synthetic depth around mid. Pump.fun has no CLOB; this is paper-only."""
        return {"symbol": symbol, "bids": [], "asks": [], "mid": 0.0, "spread_bps": 0.0}

    def snapshot_curve(self, symbol: str) -> dict[str, Any]:
        """Bonding-curve snapshot for GET /api/v1/curve (paper/mock)."""
        snap = self.get_pumpfun_snapshot(symbol)
        if snap is None:
            return {
                "symbol": symbol,
                "venue": "mock",
                "virtual_sol_reserves": None,
                "virtual_token_reserves": None,
                "curve_progress": None,
                "progress_bps": None,
                "graduated": False,
                "migrated": False,
            }
        return {
            "symbol": snap.symbol,
            "mint": snap.mint,
            "venue": "Pump.fun",
            "quote": "SOL",
            "virtual_sol_reserves": snap.virtual_sol_reserves,
            "virtual_token_reserves": snap.virtual_token_reserves,
            "real_sol_reserves": snap.real_sol_reserves,
            "real_token_reserves": snap.real_token_reserves,
            "curve_progress": snap.progress_bps / 10_000,
            "progress_bps": snap.progress_bps,
            "graduated": snap.complete,
            "migrated": snap.migrated,
            "complete": snap.complete,
            "price_sol": snap.price_sol,
            "phase": snap.phase,
            "updated_ts": snap.updated_ts,
        }
