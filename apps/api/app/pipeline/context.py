"""Build StrategyContext from mock mid/book (paper only)."""
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
    """Snapshot mock book + last mid into a StrategyContext.

    `spread_bps` overrides RiskGate liquidity (book levels stay as mocked).
    """
    provider = get_provider()
    snap: dict[str, Any] = {}
    snapshot_fn = getattr(provider, "snapshot_book", None)
    if callable(snapshot_fn):
        snap = snapshot_fn(symbol) or {}

    curve: dict[str, Any] = {}
    curve_fn = getattr(provider, "snapshot_curve", None)
    if callable(curve_fn):
        curve = curve_fn(symbol) or {}

    mid = float(snap.get("mid") or 0.0)
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

    merged_meta: dict[str, Any] = {
        "venue": curve.get("venue") or "pump.fun",
        "mint": curve.get("mint") or symbol,
        "curve_progress": curve.get("curve_progress"),
        "virtual_sol_reserves": curve.get("virtual_sol_reserves"),
        "virtual_token_reserves": curve.get("virtual_token_reserves"),
        "graduated": curve.get("graduated", False),
        "migrated": curve.get("migrated", False),
    }
    if meta:
        merged_meta.update(meta)

    return StrategyContext(
        symbol=symbol,
        ts=ts or int(time.time() * 1000),
        account=account or AccountCtx(),
        liquidity=LiquidityCtx(spread_bps=spr, adv_usd=adv_usd),
        position=position,
        meta=merged_meta,
        book=book,
        tick=TickCtx(mid=mid),
    )
