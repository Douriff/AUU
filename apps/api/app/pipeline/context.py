"""Build StrategyContext from provider mid/book + optional PumpCtx (paper only)."""
from __future__ import annotations

import time
from typing import Any, Optional

from app.models.contracts import (
    AccountCtx,
    BookCtx,
    BookLevel,
    LiquidityCtx,
    StrategyContext,
    TickCtx,
)
from app.providers import get_provider
from app.providers.mock import _base_price


def build_mock_ctx(
    symbol: str,
    *,
    ts: Optional[int] = None,
    spread_bps: Optional[float] = None,
    adv_usd: float = 100_000.0,
    meta: Optional[dict[str, Any]] = None,
    account: Optional[AccountCtx] = None,
    position: float = 0.0,
) -> StrategyContext:
    """Snapshot book + last mid into a StrategyContext.

    When the active provider is pumpfun_paper, `pump=` is filled so RiskGate
    uses bonding-curve impact instead of CEX sqrt. `spread_bps` overrides
    RiskGate liquidity (book levels stay as mocked).
    """
    provider = get_provider()
    snap: dict[str, Any] = {}
    snapshot_fn = getattr(provider, "snapshot_book", None)
    if callable(snapshot_fn):
        snap = snapshot_fn(symbol) or {}

    pump_snap = None
    get_pump = getattr(provider, "get_pumpfun_snapshot", None)
    if callable(get_pump):
        pump_snap = get_pump(symbol)

    mid = float(snap.get("mid") or 0.0)
    if mid <= 0 and pump_snap is not None:
        mid = float(pump_snap.price_sol or 0.0)
    if mid <= 0:
        candles = provider.get_candles(symbol, "1m")
        mid = float(candles[-1].c) if candles else _base_price(symbol)

    bids_raw = snap.get("bids") or []
    asks_raw = snap.get("asks") or []
    spr = float(spread_bps if spread_bps is not None else snap.get("spread_bps") or 20.0)

    book = None
    if bids_raw or asks_raw:
        book = BookCtx(
            bids=[BookLevel(price=float(x["price"]), size=float(x["size"])) for x in bids_raw],
            asks=[BookLevel(price=float(x["price"]), size=float(x["size"])) for x in asks_raw],
        )

    pump_ctx = pump_snap.to_pump_ctx() if pump_snap is not None else None
    merged_meta: dict[str, Any] = {
        "venue": "Pump.fun" if pump_ctx is not None else "mock",
        "mint": pump_snap.mint if pump_snap is not None else symbol,
    }
    if pump_snap is not None:
        merged_meta.update(
            {
                "curve_progress_bps": pump_snap.progress_bps,
                "virtual_sol_reserves": pump_snap.virtual_sol_reserves,
                "virtual_token_reserves": pump_snap.virtual_token_reserves,
                "graduated": pump_snap.complete,
                "migrated": pump_snap.migrated,
                "phase": pump_snap.phase,
            }
        )
    if meta:
        merged_meta.update(meta)

    liq_kwargs: dict[str, Any] = {"spread_bps": spr, "adv_usd": adv_usd}
    if pump_ctx is not None:
        liq_kwargs["virtual_sol_reserves"] = pump_ctx.virtual_sol_reserves
        liq_kwargs["virtual_token_reserves"] = pump_ctx.virtual_token_reserves
        liq_kwargs["real_sol_reserves"] = pump_ctx.real_sol_reserves
        liq_kwargs["real_token_reserves"] = pump_ctx.real_token_reserves
        liq_kwargs["creator_fee_bps"] = pump_ctx.creator_fee_bps
        liq_kwargs["protocol_fee_bps"] = pump_ctx.protocol_fee_bps
        liq_kwargs["fee_bps"] = pump_ctx.fee_bps

    return StrategyContext(
        symbol=symbol,
        ts=ts or int(time.time() * 1000),
        account=account or AccountCtx(),
        liquidity=LiquidityCtx(**liq_kwargs),
        position=position,
        meta=merged_meta,
        book=book,
        tick=TickCtx(mid=mid),
        pump=pump_ctx,
    )
