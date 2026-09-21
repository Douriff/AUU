"""POST /api/v1/paper/orders — PaperBroker.submit (paper mode only)."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel

from app.bus import get_hub
from app.models.contracts import OrderIntent, RiskOut, StrategyContext
from app.paper import get_paper_broker
from app.risk import get_risk_gate
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

    broker = get_paper_broker()
    fills = broker.submit(body.ctx, body.intent)
    hub = get_hub()

    if not fills:
        reject = broker.last_reject
        tags = reject.tags if reject else ["SLIPPAGE_CAP"]
        notes = reject.notes if reject else "no fill"
        get_risk_gate().on_reject(body.ctx, tags)
        await hub.publish(
            {
                "type": "reject",
                "payload": {
                    "ts": body.ctx.ts,
                    "symbol": body.ctx.symbol,
                    "tags": tags,
                    "notes": notes,
                },
            }
        )
        # Never fabricate a Fill on reject
        return ok({"fills": [], "reject": {"tags": tags, "notes": notes}})

    gate = get_risk_gate()
    fill_payloads = []
    trading_state: Optional[str] = None
    for f in fills:
        dumped = f.model_dump()
        dumped["symbol"] = body.ctx.symbol
        fill_payloads.append(dumped)
        await hub.publish({"type": "fill", "payload": dumped})
        if body.auto_post_fill:
            result = gate.post_fill(body.ctx, f)
            trading_state = result["trading_state"]
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

    data = {"fills": [f.model_dump() for f in fills]}
    if trading_state:
        data["trading_state"] = trading_state
    return ok(data)
