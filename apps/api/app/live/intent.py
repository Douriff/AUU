"""Official @pump-fun/pump-sdk intent build only. This module never submits.

Python runtime does not import the TS SDK. The dicts below are the documented
shape of PumpSdk.buyInstructions / sellInstructions so a later PR can wrap the
official MIT package — not a custom ix builder.

The send gate is in app.live.send and is independent of liveEnabled / liveDisabled.
Default runtime: intent may be described; zero chain txs are submitted.
"""
from __future__ import annotations

from typing import Any, Optional

PUMP_SDK_PACKAGE = "@pump-fun/pump-sdk"
BUY_METHOD = "buyInstructions"
SELL_METHOD = "sellInstructions"


def build_buy_intent(
    *,
    mint: str,
    sol_amount_lamports: int,
    slippage: float = 1.0,
    user: Optional[str] = None,
) -> dict[str, Any]:
    """Describe a pump-sdk buyInstructions intent. Unsigned. Not sent."""
    return {
        "sdk": PUMP_SDK_PACKAGE,
        "method": BUY_METHOD,
        "side": "buy",
        "mint": mint,
        "solAmountLamports": int(sol_amount_lamports),
        "slippage": slippage,
        "user": user,
        "unsigned": True,
        "sent": False,
    }


def build_sell_intent(
    *,
    mint: str,
    amount: int,
    slippage: float = 1.0,
    user: Optional[str] = None,
) -> dict[str, Any]:
    """Describe a pump-sdk sellInstructions intent. Unsigned. Not sent."""
    return {
        "sdk": PUMP_SDK_PACKAGE,
        "method": SELL_METHOD,
        "side": "sell",
        "mint": mint,
        "amount": int(amount),
        "slippage": slippage,
        "user": user,
        "unsigned": True,
        "sent": False,
    }


def build_intent_for_order(*, side: str, mint: str, qty_or_notional: float) -> dict[str, Any]:
    """Map an OrderIntent onto the official pump-sdk buy/sell methods only."""
    if str(side).lower() == "sell":
        return build_sell_intent(mint=mint, amount=max(0, int(qty_or_notional)))
    lamports = max(0, int(float(qty_or_notional) * 1_000_000_000))
    return build_buy_intent(mint=mint, sol_amount_lamports=lamports)
