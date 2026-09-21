"""Shared paper path: RiskGate.pre_order → PaperBroker.submit (+ optional post_fill).

Used by REST routes and the pump-paper-v1 loop so auto orders hit the same path.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from app.bus import get_hub
from app.models.contracts import OrderIntent, RiskOut, SignalOut, SizeIn, StrategyContext
from app.paper.broker import get_paper_broker
from app.paper.decision_log import (
    append_decision,
    make_row,
    shadow_fields_for_fill,
)
from app.paper.executability import annotate_fill_executability
from app.paper.guard import live_execution_blocked
from app.paper.ledger import get_paper_ledger
from app.risk import get_risk_gate

log = logging.getLogger("auu.paper.pipeline")


async def run_pre_order(
    ctx: StrategyContext,
    signal: SignalOut,
    size: SizeIn,
    *,
    strategy_id: str = "paper-manual",
) -> RiskOut:
    gate = get_risk_gate()
    risk = gate.check(ctx, signal, size)
    hub = get_hub()
    await hub.publish(
        {
            "type": "risk",
            "payload": {
                "strategyId": strategy_id,
                "symbol": ctx.symbol,
                "t": ctx.ts,
                "risk": risk.model_dump(),
            },
        }
    )
    notional = abs(float(size.target_notional))
    impact = None
    try:
        impact = float(
            ctx.liquidity.estimated_impact_bps(
                notional, side=("buy" if signal.side == "long" else "sell"), pump=ctx.pump
            )
        )
    except (ValueError, TypeError, ZeroDivisionError):
        impact = None
    mint = None
    if ctx.meta:
        mint = ctx.meta.get("mint")
    append_decision(
        make_row(
            ts=ctx.ts,
            strategy_id=strategy_id,
            symbol=ctx.symbol,
            mint=str(mint) if mint else None,
            stage="pre_order",
            outcome="reject" if not risk.allow else "emit_signal",
            signal=signal,
            risk=risk,
            notional_sol=notional,
            impact_bps_est=impact,
            impact_bps_cap=float(size.max_slippage_bps),
            estimated_impact_bps=impact,
            pump=ctx.pump,
            liquidity=ctx.liquidity,
            decision_px=float(ctx.tick.mid) if ctx.tick and ctx.tick.mid else None,
            arrival_px=float(ctx.tick.mid) if ctx.tick and ctx.tick.mid else None,
        )
    )
    if not risk.allow:
        gate.on_reject(ctx, risk.tags)
        await hub.publish(
            {
                "type": "reject",
                "payload": {
                    "ts": ctx.ts,
                    "symbol": ctx.symbol,
                    "tags": risk.tags,
                    "notes": risk.notes or "",
                },
            }
        )
    return risk


async def run_paper_order(
    ctx: StrategyContext,
    intent: OrderIntent,
    risk: RiskOut,
    *,
    auto_post_fill: bool = True,
    close_reason: str = "",
) -> dict[str, Any]:
    """Submit to PaperBroker. Never fabricates a Fill when denied / empty."""
    blocked, why = live_execution_blocked()
    if blocked:
        log.warning("paper submit refused: %s", why)
        mint = ctx.meta.get("mint") if ctx.meta else None
        append_decision(
            make_row(
                ts=ctx.ts,
                strategy_id=intent.client_tag or "paper",
                symbol=ctx.symbol,
                mint=str(mint) if mint else None,
                stage="live_blocked",
                outcome="reject",
                signal_side=intent.side,
                signal_reason="LIVE_DISABLED",
                risk_allow=False,
                risk_tags=["LIVE_DISABLED"],
                risk_notes=why,
                notional_sol=abs(float(intent.qty_or_notional)),
                reject_bucket="risk",
                pump=ctx.pump,
                liquidity=ctx.liquidity,
            )
        )
        return {"fills": [], "reject": {"tags": ["LIVE_DISABLED"], "notes": why}}
    if not risk.allow:
        return {"fills": [], "reject": {"tags": ["RISK_DENIED"], "notes": "risk.allow must be true"}}

    broker = get_paper_broker()
    fills = [
        annotate_fill_executability(f, ctx, intent) for f in broker.submit(ctx, intent)
    ]
    hub = get_hub()

    if not fills:
        reject = broker.last_reject
        tags = reject.tags if reject else ["SLIPPAGE_CAP"]
        notes = reject.notes if reject else "no fill"
        get_risk_gate().on_reject(ctx, tags)
        mint = ctx.meta.get("mint") if ctx.meta else None
        append_decision(
            make_row(
                ts=ctx.ts,
                strategy_id=intent.client_tag or "paper",
                symbol=ctx.symbol,
                mint=str(mint) if mint else None,
                stage="paper_submit",
                outcome="reject",
                signal_side=intent.side,
                signal_reason=close_reason or (tags[0] if tags else "reject"),
                risk_allow=False,
                risk_tags=tags,
                risk_notes=notes,
                notional_sol=abs(float(intent.qty_or_notional)),
                pump=ctx.pump,
                liquidity=ctx.liquidity,
            )
        )
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
    fill_payloads: list[dict[str, Any]] = []
    trading_state: Optional[str] = None
    ledger = get_paper_ledger()
    mint = None
    if ctx.meta:
        mint = ctx.meta.get("mint")
    for f in fills:
        dumped = f.model_dump()
        dumped["symbol"] = ctx.symbol
        fill_payloads.append(dumped)
        ledger.record_fill(ctx.symbol, f, reason=close_reason, mint=str(mint) if mint else None)
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
    first = fills[0]
    mint = ctx.meta.get("mint") if ctx.meta else None
    impact = first.estimated_impact_bps
    decision_px = float(ctx.tick.mid) if ctx.tick and ctx.tick.mid else None
    shadow = shadow_fields_for_fill(
        first, ctx.symbol, impact, decision_px=decision_px, side=intent.side
    )
    append_decision(
        make_row(
            ts=first.ts,
            strategy_id=intent.client_tag or "paper",
            symbol=ctx.symbol,
            mint=str(mint) if mint else None,
            stage="paper_submit",
            outcome="fill" if len(fills) == 1 else "partial",
            signal_side=intent.side,
            signal_reason=close_reason or "fill",
            risk_allow=True,
            risk_tags=list(risk.tags or []),
            risk_notes=risk.notes,
            notional_sol=abs(float(intent.qty_or_notional)),
            impact_bps_est=impact,
            impact_bps_cap=float(intent.max_slippage_bps),
            estimated_impact_bps=shadow.get("estimated_impact_bps"),
            pump=ctx.pump,
            liquidity=ctx.liquidity,
            decision_px=shadow.get("decision_px"),
            arrival_px=shadow.get("arrival_px"),
            fill_px=shadow.get("fill_px"),
            paper_fill_px=shadow.get("paper_fill_px"),
            shadow_fill_px=shadow.get("shadow_fill_px"),
            shadow_slippage_bps=shadow.get("shadow_slippage_bps"),
            impact_error_bps=shadow.get("impact_error_bps"),
            shadow_source=shadow.get("shadow_source"),
        )
    )
    return data
