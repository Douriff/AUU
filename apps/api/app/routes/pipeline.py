"""POST /api/v1/pipeline/decide-and-fill — one-shot mock/pump ctx → risk → paper fill."""
from __future__ import annotations

from typing import Any, Literal, Optional

from fastapi import APIRouter
from pydantic import BaseModel

from app.models.contracts import AccountCtx, SignalOut, SizeIn, StrategyContext
from app.pipeline import decide_and_fill
from app.providers import default_symbol
from app.routes.envelope import err, ok

router = APIRouter(prefix="/api/v1", tags=["pipeline"])


class DecideAndFillBody(BaseModel):
    symbol: str = ""
    side: Literal["buy", "sell"] = "buy"
    notional: float = 0.1  # SOL on pumpfun_paper; quote units otherwise
    max_slippage_bps: float = 150.0
    strategyId: str = "pipeline-v0"
    # optional overrides — frozen SignalOut / SizeIn / StrategyContext unchanged
    signal: Optional[SignalOut] = None
    size: Optional[SizeIn] = None
    ctx: Optional[StrategyContext] = None
    spread_bps: Optional[float] = None
    meta: Optional[dict[str, Any]] = None
    account: Optional[AccountCtx] = None
    auto_post_fill: bool = True


@router.post("/pipeline/decide-and-fill")
async def pipeline_decide_and_fill(body: DecideAndFillBody):
    if body.notional <= 0 and body.size is None:
        return err("BAD_REQUEST", "notional must be > 0", 400)
    symbol = body.symbol or default_symbol()
    if not symbol:
        return err("BAD_REQUEST", "symbol required", 400)

    data = await decide_and_fill(
        symbol=symbol,
        side=body.side,
        notional=body.notional,
        max_slippage_bps=body.max_slippage_bps,
        strategy_id=body.strategyId,
        signal=body.signal,
        size=body.size,
        ctx=body.ctx,
        spread_bps=body.spread_bps,
        meta=body.meta,
        account=body.account,
        auto_post_fill=body.auto_post_fill,
    )
    return ok(data)
