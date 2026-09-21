"""Shared paper submit after RiskGate allow — used by /paper/orders and pipeline."""
from __future__ import annotations

from typing import Any, Optional

from app.bus import get_hub
from app.models.contracts import OrderIntent, RiskOut, StrategyContext
from app.paper.broker import get_paper_broker
from app.risk import get_risk_gate


async def execute_paper_order(
    ctx: StrategyContext,
    intent: OrderIntent,
    risk: RiskOut,
    *,
    auto_post_fill: bool = True,
) -> dict[str, Any]:
    """Submit to PaperBroker. Never fabricates a Fill on reject.

    Caller must ensure risk.allow is True (route returns RISK_DENIED otherwise).
    """
    if not risk.allow:
        return {
            "fills": [],
            "reject": {"tags": risk.tags, "notes": risk.notes or "risk.allow is false"},
        }

    broker = get_paper_broker()
    fills = broker.submit(ctx, intent)
    hub = get_hub()

    if not fills:
        reject = broker.last_reject
        tags = reject.tags if reject else ["SLIPPAGE_CAP"]
        notes = reject.notes if reject else "no fill"
        get_risk_gate().on_reject(ctx, tags)
        await hub.publish(
            {
                "type": "reject",
                "payload": {
                    "ts": ctx.ts,
                    "symbol": ctx.symbol,
                    "tags": tags,
                    "notes": notes,
                },
            }
        )
        return {"fills": [], "reject": {"tags": tags, "notes": notes}}

    gate = get_risk_gate()
    trading_state: Optional[str] = None
    for f in fills:
        dumped = f.model_dump()
        dumped["symbol"] = ctx.symbol
        await hub.publish({"type": "fill", "payload": dumped})
        if auto_post_fill:
            result = gate.post_fill(ctx, f)
            trading_state = result["trading_state"]
            if result.get("state_changed"):
                await hub.publish(
                    {
                        "type": "trading_state",
                        "payload": {
                            "state": result["trading_state"],
                            "reason": result.get("reason"),
                            "symbol": ctx.symbol,
                            "ts": ctx.ts,
                        },
                    }
                )

    data: dict[str, Any] = {"fills": [f.model_dump() for f in fills]}
    if trading_state:
        data["trading_state"] = trading_state
    return data
