"""Pump.fun paper MarketDataProvider.

Venue: **pump.fun** (Solana bonding curve), not a CEX spot book.

`DATA_PROVIDER=pumpfun_paper` (also accepted: pumpfun / pump.fun).

v0 behaviour
------------
Delegates candles / synth book / trades / demo signals to the deterministic
mock curve simulation (virtual SOL + token reserves, curve_progress,
graduated / migrated). Paper matching still goes through RiskGate + PaperBroker.

Explicitly out of scope (do not implement here)
-----------------------------------------------
- Wallet / private keys / seed phrases
- Live tx, auto-buy, sniper, copy-trade
- Pump.fun authenticated APIs

Later (read-only only)
----------------------
Public curve account data / unofficial public HTTP for reserves + progress.
Still no keys, still paper fills only.
"""
from __future__ import annotations

from app.providers.mock import MockMarketDataProvider
from app.providers.pump_mints import VENUE, curve_for_symbol


class PumpFunPaperProvider(MockMarketDataProvider):
    """Paper bonding-curve venue slot. v0 = mock curve sim, not a live adapter."""

    name = "pumpfun_paper"
    venue = VENUE

    def snapshot_curve(self, symbol: str) -> dict:
        return curve_for_symbol(symbol)

    def connect_live(self) -> None:
        raise RuntimeError(
            "live Pump.fun is disabled: paper/mock only (no keys, no sniper, no auto-buy)"
        )
