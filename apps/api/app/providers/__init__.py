from .base import MarketDataProvider
from .mock import MockMarketDataProvider

AVAILABLE_PROVIDERS = ("mock", "pumpfun_paper")

_provider: MarketDataProvider | None = None


def get_provider() -> MarketDataProvider:
    """Select market data provider from DATA_PROVIDER=mock|pumpfun_paper."""
    global _provider
    if _provider is None:
        import os

        name = os.getenv("DATA_PROVIDER", "mock").lower().strip()
        if name == "pumpfun_paper":
            from .pumpfun_paper import PumpfunPaperProvider

            _provider = PumpfunPaperProvider()
        else:
            _provider = MockMarketDataProvider()
    return _provider


def reset_provider() -> None:
    global _provider
    _provider = None


__all__ = [
    "AVAILABLE_PROVIDERS",
    "MarketDataProvider",
    "MockMarketDataProvider",
    "get_provider",
    "reset_provider",
]
