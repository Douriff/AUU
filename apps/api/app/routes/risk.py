"""POST /api/v1/risk/pre-order and /api/v1/risk/post-fill."""
from __future__ import annotations

import time
from typing import Any, Optional

from fastapi import APIRouter
from pydantic import BaseModel

from app.bus import get_hub
from app.models.contracts import (
    AccountCtx,
    Fill,
    LiquidityCtx,
    PumpCtx,
    SignalOut,
    SizeIn,
    StrategyContext,
)
from app.paper.pipeline import run_pre_order
from app.risk import get_risk_gate
from app.routes.envelope import err, ok

router = APIRouter(prefix="/api/v1", tags=["risk"])


class PreOrderBody(BaseModel):
    """Accept full {ctx, signal, size} or flattened symbol/ts/account/liquidity/size/meta."""

    ctx: Optional[StrategyContext] = None
    signal: Optional[SignalOut] = None
    size: Optional[SizeIn] = None
    # flattened shortcuts
    symbol: Optional[str] = None
    ts: Optional[int] = None
    account: Optional[AccountCtx] = None
    liquidity: Optional[LiquidityCtx] = None
    meta: Optional[dict[str, Any]] = None
    position: Optional[float] = None
    features: Optional[dict[str, Any]] = None
    pump: Optional[PumpCtx] = None


class PostFillBody(BaseModel):
    ctx: StrategyContext
    fill: Fill


def _build_ctx(body: PreOrderBody) -> StrategyContext:
    if body.ctx is not None:
        return body.ctx
    if not body.symbol:
        raise ValueError("symbol or ctx required")
    return StrategyContext(
        symbol=body.symbol,
        ts=body.ts or int(time.time() * 1000),
        account=body.account or AccountCtx(),
        liquidity=body.liquidity or LiquidityCtx(),
        position=body.position or 0.0,
        features=body.features or {},
        meta=body.meta or {},
        pump=body.pump,
    )


@router.post("/risk/pre-order")
async def pre_order(body: PreOrderBody):
    try:
        ctx = _build_ctx(body)
    except ValueError as e:
        return err("BAD_REQUEST", str(e), 400)

    signal = body.signal or SignalOut(side="long", strength=0.5, reason="manual")
    size = body.size
    if size is None:
        # allow raw size dict via flattened — default demo notional
        size = SizeIn(target_notional=500.0, max_slippage_bps=150.0)

    risk = await run_pre_order(ctx, signal, size, strategy_id="paper-manual")
    return ok(risk.model_dump())


@router.post("/risk/post-fill")
async def post_fill(body: PostFillBody):
    gate = get_risk_gate()
    result = gate.post_fill(body.ctx, body.fill)

    hub = get_hub()
    if result.get("state_changed"):
        await hub.publish(
            {
                "type": "trading_state",
                "payload": {
                    "state": result["trading_state"],
                    "reason": result.get("reason"),
                    "symbol": body.ctx.symbol,
                    "ts": body.ctx.ts,
                },
            }
        )

    return ok(
        {
            "trading_state": result["trading_state"],
            "tags": result.get("tags", []),
            "notes": result.get("notes", ""),
        }
    )
