"""Mainstream-coin market data (BTC/ETH/SOL by default) from public CEX endpoints.

ccxt (MIT) against Binance / OKX public REST only: no API keys, no private
endpoints, no order calls. Klines (1d, 1h) and perpetual funding-rate history
are stored in a local SQLite file with incremental updates, gap repair and
retries. Read-only HTTP lives in :mod:`app.routes.mainstream`.
"""
from app.marketdata.mainstream.config import MainstreamConfig, load_config
from app.marketdata.mainstream.service import MainstreamService, get_service, reset_service
from app.marketdata.mainstream.store import MarketStore

__all__ = [
    "MainstreamConfig",
    "MainstreamService",
    "MarketStore",
    "get_service",
    "load_config",
    "reset_service",
]
