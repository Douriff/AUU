"""Curve quotes for real-market paper fills.

Uses the same bonding-curve helpers as the rest of the paper stack
(protocol fee ``DEFAULT_PROTOCOL_FEE_BPS``). No chain send, no keys.
"""
from __future__ import annotations

from typing import Optional

from app.providers.pumpfun_curve_math import (
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
        px = sol_lamports / tokens
        qty = tokens / LAMPORTS_PER_SOL
        fee_lamports = sol_lamports - sol_after_buy_fee(sol_lamports, proto, creator)
        fee = max(0, fee_lamports) / LAMPORTS_PER_SOL
    elif direction == "sell":
        held = abs(float(flatten_qty or 0.0))
        raw = int(round(held * LAMPORTS_PER_SOL))
        if held <= 0 or raw <= 0:
            return None
        net, gross = sell_sol_out(vs, vt, raw, proto, creator)
        if gross <= 0 or net < 0:
            return None
        px = net / raw
        qty = held
        fee = max(0, gross - net) / LAMPORTS_PER_SOL
    else:
        return None

    if px <= 0 or qty <= 0:
        return None
    slip = abs(px - spot) / spot * 1e4 if spot > 0 else 0.0
    return px, qty, fee, slip
