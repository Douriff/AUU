from __future__ import annotations

import os

from .base import MarketDataProvider
from .mock import MockMarketDataProvider

AVAILABLE_PROVIDERS = ("mock", "pumpfun_paper", "pumpfun_live_paper")

_provider: MarketDataProvider | None = None


def get_provider() -> MarketDataProvider:
    """Select market data from DATA_PROVIDER=mock|pumpfun_paper|pumpfun_live_paper.

    Unknown names fall back to mock. No provider loads a wallet or sends a transaction.
    """
    global _provider
    if _provider is None:
        name = os.getenv("DATA_PROVIDER", "mock").lower().replace("-", "_").strip()
        if name in {"pumpfun_live_paper", "pumpfun_live", "live_paper"}:
            from .pumpfun_live_paper import PumpfunLivePaperProvider

            _provider = PumpfunLivePaperProvider()
        elif name in {"pumpfun_paper", "pumpfun", "pump.fun", "pump_fun"}:
            from .pumpfun_paper import PumpfunPaperProvider

            _provider = PumpfunPaperProvider()
        else:
            _provider = MockMarketDataProvider()
    return _provider


def reset_provider() -> None:
    global _provider
    _provider = None


def market_data_kind(name: str | None = None) -> str:
    """``real`` | ``synthetic`` | ``mock`` for the active (or named) provider."""
    provider_name = name if name is not None else get_provider().name
    if provider_name == "pumpfun_live_paper":
        return "real"
    if provider_name == "pumpfun_paper":
        return "synthetic"
    return "mock"


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
    "market_data_kind",
    "reset_provider",
]
