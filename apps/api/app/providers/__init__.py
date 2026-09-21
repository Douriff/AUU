from __future__ import annotations

import os

from .base import MarketDataProvider
from .mock import MockMarketDataProvider
from .pumpfun import PumpFunPaperProvider

__all__ = [
    "MarketDataProvider",
    "MockMarketDataProvider",
    "PumpFunPaperProvider",
    "get_provider",
]

_provider: MarketDataProvider | None = None


def get_provider() -> MarketDataProvider:
    """Resolve DATA_PROVIDER. Unknown / live names fall back to mock — never live keys."""
    global _provider
    if _provider is not None:
        return _provider
    name = os.getenv("DATA_PROVIDER", "mock").lower().replace("-", "_")
    if name in {"pumpfun_paper", "pumpfun", "pump.fun", "pump_fun"}:
        _provider = PumpFunPaperProvider()
    else:
        _provider = MockMarketDataProvider()
    return _provider
