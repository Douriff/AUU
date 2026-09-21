from __future__ import annotations

import os

from .base import MarketDataProvider
from .mock import MockMarketDataProvider

AVAILABLE_PROVIDERS = ("mock", "pumpfun_paper")

_provider: MarketDataProvider | None = None


def get_provider() -> MarketDataProvider:
    """Select market data provider from DATA_PROVIDER=mock|pumpfun_paper.

    Unknown / live names fall back to mock — never live keys.
    """
    global _provider
    if _provider is None:
        name = os.getenv("DATA_PROVIDER", "mock").lower().replace("-", "_").strip()
        if name in {"pumpfun_paper", "pumpfun", "pump.fun", "pump_fun"}:
            from .pumpfun_paper import PumpfunPaperProvider

            _provider = PumpfunPaperProvider()
        else:
            _provider = MockMarketDataProvider()
    return _provider


def reset_provider() -> None:
    global _provider
    _provider = None


def default_symbol() -> str:
    """First listed symbol for the active provider (Trade / pipeline default)."""
    provider = get_provider()
    if provider.name == "pumpfun_paper":
        return "PUMPDEMO/SOL"
    syms = provider.list_symbols()
    return syms[0].symbol if syms else "MOCK/USDC"


__all__ = [
    "AVAILABLE_PROVIDERS",
    "MarketDataProvider",
    "MockMarketDataProvider",
    "default_symbol",
    "get_provider",
    "reset_provider",
]
