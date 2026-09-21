"""Optional enrich (Solana Tracker / Bitquery) — env-key gated, default off.

Never auto-adds wallets. Never scrapes Photon / BullX / GMGN / Pump frontend.
P0 snapshot truth remains Helius/RPC + ctx.pump.
"""
from __future__ import annotations

import os
from typing import Any


def _truthy(raw: str) -> bool:
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def tracker_key_present() -> bool:
    return bool((os.getenv("SOLANA_TRACKER_API_KEY") or "").strip())


def bitquery_key_present() -> bool:
    return bool((os.getenv("BITQUERY_API_KEY") or os.getenv("BITQUERY_TOKEN") or "").strip())


def enrich_mode() -> str:
    raw = (os.getenv("TRADER_WATCH_ENRICH") or "off").strip().lower()
    if raw in {"tracker", "solanatracker"} and tracker_key_present():
        return "tracker"
    if raw in {"bitquery"} and bitquery_key_present():
        return "bitquery"
    return "off"


def enrich_enabled() -> bool:
    return enrich_mode() != "off"


def leaderboard_candidates() -> list[dict[str, Any]]:
    """M3 stub: never hits network; never writes Watchlist."""
    return []
