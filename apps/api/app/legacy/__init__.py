"""Legacy feature switches.

``AUU_LEGACY_PUMP`` (default ``off``) re-enables the archived pump.fun / Solana
stack under :mod:`app.legacy.pump` — provider feed, discovery, pump-paper-v1
loop, trader watch, wallet and their HTTP routes. With it off, none of that is
mounted or started; the mainstream (CEX public market data) stack runs instead.
"""
from __future__ import annotations

import os

_TRUE = {"1", "true", "on", "yes"}


def legacy_pump_enabled() -> bool:
    return os.getenv("AUU_LEGACY_PUMP", "off").strip().lower() in _TRUE


__all__ = ["legacy_pump_enabled"]
