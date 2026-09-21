"""HeliusTraderReader — P0 readonly stub, default OFF.

P0 truth source (research): user-selected wallets + on-chain/Helius parsed txs.
This module does not scrape pump.fun frontend APIs, does not submit chain
transactions, and does not embed API keys. HELIUS_API_KEY is env-only and never logged.
"""
from __future__ import annotations

import os
from typing import Optional

from app.models.contracts import TraderSnapshot, TraderWatchlistItem

# Documented Helius parsed-tx path (not called in P0 stub).
HELIUS_PARSED_TX_PATH = "/v0/addresses/{address}/transactions"
DEFAULT_HELIUS_HOST = "https://api.helius.xyz"


def reader_mode() -> str:
    raw = (os.getenv("TRADER_WATCH_READER") or "mock").strip().lower()
    if raw in {"helius", "indexer"}:
        return "helius"
    return "mock"


def helius_enabled() -> bool:
    return reader_mode() == "helius"


def _api_key_present() -> bool:
    return bool((os.getenv("HELIUS_API_KEY") or "").strip())


class HeliusTraderReader:
    """Readonly reader stub. Live HTTP fetch is not wired in this P0 scaffold."""

    name = "helius"
    LIVE_FETCH = False

    def __init__(self) -> None:
        self._key_configured = _api_key_present()

    def fetch_snapshot(
        self, item: TraderWatchlistItem, *, now_ms: Optional[int] = None
    ) -> TraderSnapshot:
        # Default-off live path: even when TRADER_WATCH_READER=helius, P0 does not
        # hit the network (no pump frontend, no chain submit). Mock tape keeps
        # paper distill tests deterministic.
        from app.traders.snapshot import mock_snapshot_for

        return mock_snapshot_for(item, now_ms=now_ms)

    def key_configured(self) -> bool:
        return self._key_configured
