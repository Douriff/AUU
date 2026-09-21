"""Pump.fun mock mint catalog — paper/sim only, not real Solana addresses.

Venue: pump.fun (Solana bonding curve). Quote is SOL, identity is mint-like id.
These strings are labeled `Pmp…` so they cannot be confused with live mints.
"""
from __future__ import annotations

from typing import Any, TypedDict

from app.models.contracts import SymbolInfo

VENUE = "pump.fun"
QUOTE = "SOL"

# Constant-product virtual reserves (pump.fun-style). Paper numbers only.
INITIAL_VIRTUAL_SOL = 30.0
INITIAL_VIRTUAL_TOKEN = 1_073_000_000.0
GRADUATION_SOL = 85.0

# Default watchlist / Trade / pipeline symbol (pump-style mint id, not CEX pair).
DEFAULT_SYMBOL = "PmpPEPE11111111111111111111111111111111111"


class PumpMintSpec(TypedDict):
    ticker: str
    mint: str
    progress: float
    graduated: bool
    migrated: bool


PUMP_MINTS: list[PumpMintSpec] = [
    {
        "ticker": "PEPE",
        "mint": DEFAULT_SYMBOL,
        "progress": 0.42,
        "graduated": False,
        "migrated": False,
    },
    {
        "ticker": "DOGE",
        "mint": "PmpDOGE11111111111111111111111111111111111",
        "progress": 0.18,
        "graduated": False,
        "migrated": False,
    },
    {
        "ticker": "WIF",
        "mint": "PmpWIF111111111111111111111111111111111111",
        "progress": 0.91,
        "graduated": False,
        "migrated": False,
    },
    {
        "ticker": "BONK",
        "mint": "PmpBONK11111111111111111111111111111111111",
        "progress": 1.0,
        "graduated": True,
        "migrated": True,
    },
    {
        "ticker": "FROG",
        "mint": "PmpFROG11111111111111111111111111111111111",
        "progress": 0.07,
        "graduated": False,
        "migrated": False,
    },
]


def curve_state(progress: float, *, graduated: bool = False, migrated: bool = False) -> dict[str, Any]:
    """Virtual AMM snapshot. k = virt_sol * virt_token."""
    p = min(max(float(progress), 0.0), 1.0)
    virt_sol = INITIAL_VIRTUAL_SOL + p * (GRADUATION_SOL - INITIAL_VIRTUAL_SOL)
    k = INITIAL_VIRTUAL_SOL * INITIAL_VIRTUAL_TOKEN
    virt_tok = k / max(virt_sol, 1e-12)
    price = virt_sol / virt_tok
    return {
        "venue": VENUE,
        "virtual_sol_reserves": virt_sol,
        "virtual_token_reserves": virt_tok,
        "real_sol_reserves": max(0.0, virt_sol - INITIAL_VIRTUAL_SOL),
        "curve_progress": p,
        "graduated": graduated,
        "migrated": migrated,
        "price_sol": price,
        "graduation_sol": GRADUATION_SOL,
        "quote": QUOTE,
    }


def spec_for(symbol: str) -> PumpMintSpec | None:
    for spec in PUMP_MINTS:
        if spec["mint"] == symbol or spec["ticker"] == symbol:
            return spec
    return None


def curve_for_symbol(symbol: str) -> dict[str, Any]:
    spec = spec_for(symbol)
    if spec is None:
        st = curve_state(0.1)
        st["symbol"] = symbol
        st["mint"] = symbol
        st["base"] = symbol[:8]
        return st
    st = curve_state(spec["progress"], graduated=spec["graduated"], migrated=spec["migrated"])
    st["symbol"] = spec["mint"]
    st["mint"] = spec["mint"]
    st["base"] = spec["ticker"]
    return st


def price_sol(symbol: str) -> float:
    return float(curve_for_symbol(symbol)["price_sol"])


def symbol_infos() -> list[SymbolInfo]:
    out: list[SymbolInfo] = []
    for spec in PUMP_MINTS:
        st = curve_state(spec["progress"], graduated=spec["graduated"], migrated=spec["migrated"])
        out.append(
            SymbolInfo(
                symbol=spec["mint"],
                base=spec["ticker"],
                quote=QUOTE,
                kind="pumpfun_bonding" if not spec["graduated"] else "pumpfun_migrated",
                mint=spec["mint"],
                venue=VENUE,
                curve_progress=st["curve_progress"],
                virtual_sol_reserves=st["virtual_sol_reserves"],
                virtual_token_reserves=st["virtual_token_reserves"],
                graduated=spec["graduated"],
                migrated=spec["migrated"],
            )
        )
    return out
