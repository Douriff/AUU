"""POST /api/v1/paper/orders — PaperBroker.submit (paper mode only)."""
from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from app.models.contracts import OrderIntent, RiskOut, StrategyContext
from app.paper.pipeline import run_paper_order
from app.routes.envelope import err, ok

router = APIRouter(prefix="/api/v1", tags=["paper"])


class PaperOrderBody(BaseModel):
    ctx: StrategyContext
    intent: OrderIntent
    risk: RiskOut
    # optional: auto post_fill after each fill
    auto_post_fill: bool = True


@router.post("/paper/orders")
async def paper_orders(body: PaperOrderBody):
    if not body.risk.allow:
        return err("RISK_DENIED", "risk.allow must be true before paper submit", 400)

    data = await run_paper_order(
        body.ctx, body.intent, body.risk, auto_post_fill=body.auto_post_fill
    )
    return ok(data)
