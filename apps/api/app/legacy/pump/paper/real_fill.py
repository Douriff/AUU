"""Curve quotes for real-market paper fills.

Uses the same bonding-curve helpers as the rest of the paper stack. No chain
send, no keys.

Fee convention (same as the ledger): ``price`` EXCLUDES fees and ``fee`` is
reported separately, so ``price * qty + fee`` is the SOL spent on a buy and
``price * qty - fee`` is the SOL received on a sell. The ledger subtracts
``fee`` once; embedding it in ``price`` too would count it twice.
"""
from __future__ import annotations

import os
from typing import Any, Mapping, Optional

from app.legacy.pump.providers.pumpfun_curve_math import (
    DEFAULT_CREATOR_FEE_BPS,
    DEFAULT_PROTOCOL_FEE_BPS,
    LAMPORTS_PER_SOL,
    buy_tokens_out,
    price_sol,
    sell_sol_out,
    sol_after_buy_fee,
)


def quote_curve_fill(
    *,
    side: str,
    notional_sol: float,
    virtual_sol_reserves: int,
    virtual_token_reserves: int,
    real_token_reserves: int,
    flatten_qty: Optional[float] = None,
    protocol_fee_bps: int = DEFAULT_PROTOCOL_FEE_BPS,
    creator_fee_bps: int = DEFAULT_CREATOR_FEE_BPS,
    mid: float = 0.0,
) -> Optional[tuple[float, float, float, float]]:
    """Return ``(price, abs_qty, fee_sol, slippage_bps)`` or None when the curve cannot fill.

    ``price`` is in the same units as ``price_sol`` (lamports per raw token).
    ``abs_qty`` is such that ``qty * price`` is SOL. A sell's ``abs_qty`` is the
    held token quantity (``flatten_qty``), not notional divided by price.
    """
    vs = int(virtual_sol_reserves)
    vt = int(virtual_token_reserves)
    rt = int(real_token_reserves)
    proto = int(protocol_fee_bps)
    creator = int(creator_fee_bps)
    direction = (side or "").strip().lower()
    spot = float(mid) if mid and mid > 0 else price_sol(vs, vt)

    if direction == "buy":
        sol_lamports = int(abs(float(notional_sol)) * LAMPORTS_PER_SOL)
        tokens = buy_tokens_out(vs, vt, rt, sol_lamports, proto, creator)
        if tokens <= 0 or sol_lamports <= 0:
            return None
        after_fee = sol_after_buy_fee(sol_lamports, proto, creator)
        px = after_fee / tokens
        qty = tokens / LAMPORTS_PER_SOL
        fee = max(0, sol_lamports - after_fee) / LAMPORTS_PER_SOL
    elif direction == "sell":
        held = abs(float(flatten_qty or 0.0))
        raw = int(round(held * LAMPORTS_PER_SOL))
        if held <= 0 or raw <= 0:
            return None
        net, gross = sell_sol_out(vs, vt, raw, proto, creator)
        if gross <= 0 or net < 0:
            return None
        px = gross / raw
        qty = held
        fee = max(0, gross - net) / LAMPORTS_PER_SOL
    else:
        return None

    if px <= 0 or qty <= 0:
        return None
    slip = abs(px - spot) / spot * 1e4 if spot > 0 else 0.0
    return px, qty, fee, slip


def _int_or_none(val: Any) -> Optional[int]:
    if val is None or val == "":
        return None
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


# Conservative fallback when no fee was observed on a real TradeEvent (the
# PumpPortal trade feed carries no fee fields). Current pump.fun bonding-curve
# fees are ~95 bps protocol + ~30 bps creator; unknown must never understate.
UNKNOWN_PROTOCOL_FEE_BPS = 95
UNKNOWN_CREATOR_FEE_BPS = 30


def curve_fee_bps(meta: Mapping[str, Any], pump: Any = None) -> tuple[int, int]:
    """(protocol, creator) fee bps for a real-market curve fill.

    Each component independently: the bps observed on the latest real
    TradeEvent (``meta``) wins. When unobserved, use the conservative default
    (95 protocol / 30 creator), raised (never lowered) by
    ``PAPER_CURVE_PROTOCOL_FEE_BPS`` / ``PAPER_CURVE_CREATOR_FEE_BPS`` or a
    larger snapshot creator fee.
    """
    proto = _int_or_none(meta.get("protocol_fee_bps"))
    if proto is None:
        env = _int_or_none(os.getenv("PAPER_CURVE_PROTOCOL_FEE_BPS"))
        proto = max(UNKNOWN_PROTOCOL_FEE_BPS, env or 0)
    creator = _int_or_none(meta.get("creator_fee_bps"))
    if creator is None:
        env = _int_or_none(os.getenv("PAPER_CURVE_CREATOR_FEE_BPS"))
        snap_bps = _int_or_none(getattr(pump, "creator_fee_bps", None))
        creator = max(UNKNOWN_CREATOR_FEE_BPS, env or 0, snap_bps or 0)
    return max(0, proto), max(0, creator)
