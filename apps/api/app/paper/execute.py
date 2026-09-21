"""Shared paper submit after RiskGate allow — used by /paper/orders and pipeline.

Delegates to `run_paper_order` so Trade decide-and-fill and pump-paper-v1 auto
orders share one PaperBroker path (never fabricates a Fill on reject).
"""
from __future__ import annotations

from typing import Any

from app.models.contracts import OrderIntent, RiskOut, StrategyContext
from app.paper.pipeline import run_paper_order


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
    return await run_paper_order(ctx, intent, risk, auto_post_fill=auto_post_fill)
