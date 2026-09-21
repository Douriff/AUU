"""Trader Watch → Habit → Distill (paper observe only).

Never mirrors wallets. Never sends chain transactions. No private keys.
copy_trade_enabled is hard-false; there is no mirror path.
"""
from __future__ import annotations

from app.traders.distill import apply_distill, distill_watch
from app.traders.habits import habit_profile
from app.traders.helius import HeliusTraderReader, reader_mode
from app.traders.snapshot import get_snapshot
from app.traders.store import (
    delete_watch,
    get_watch,
    list_watches,
    reset_watch_store,
    upsert_watch,
)

COPY_TRADE_ENABLED: bool = False

__all__ = [
    "COPY_TRADE_ENABLED",
    "HeliusTraderReader",
    "apply_distill",
    "delete_watch",
    "distill_watch",
    "get_snapshot",
    "get_watch",
    "habit_profile",
    "list_watches",
    "reader_mode",
    "reset_watch_store",
    "upsert_watch",
]
