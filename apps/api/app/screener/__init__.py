"""Isolated read-only spot screener. Does not place orders or start a market WebSocket."""

from app.screener.logic import LIVE_ENABLED

__all__ = ["LIVE_ENABLED"]
