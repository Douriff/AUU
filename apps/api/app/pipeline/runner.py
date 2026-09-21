"""One-shot Signal → RiskGate → PaperBroker using mock/pump ctx."""
from __future__ import annotations

from typing import Any, Literal, Optional

from app.bus import get_hub
from app.models.contracts import (
    AccountCtx,
    OrderIntent,
    SignalOut,
    SizeIn,
    StrategyContext,
)
from app.paper.decision_log import append_decision, make_row
from app.paper.execute import execute_paper_order
from app.pipeline.context import build_mock_ctx
from app.risk import get_risk_gate


async def decide_and_fill(
    *,
    symbol: str,
    side: Literal["buy", "sell"] = "buy",
    notional: float = 0.1,
    max_slippage_bps: float = 150.0,
    strategy_id: str = "pipeline-v0",
    signal: Optional[SignalOut] = None,
    size: Optional[SizeIn] = None,
    ctx: Optional[StrategyContext] = None,
    spread_bps: Optional[float] = None,
    meta: Optional[dict[str, Any]] = None,
    account: Optional[AccountCtx] = None,
    auto_post_fill: bool = True,
) -> dict[str, Any]:
    """Build ctx from provider mid/book/pump (unless provided), emit WS, fill or reject."""
    built = ctx or build_mock_ctx(
        symbol,
        spread_bps=spread_bps,
        meta=meta,
        account=account,
    )
    sig = signal or SignalOut(
        side="long" if side == "buy" else "short",
        strength=0.6,
        reason="pipeline-decide",
        tags=["pipeline"],
    )
    sz = size or SizeIn(target_notional=float(notional), max_slippage_bps=max_slippage_bps)

    hub = get_hub()
    await hub.publish(
        {
            "type": "signal",
            "payload": {
                "strategyId": strategy_id,
                "symbol": built.symbol,
                "t": built.ts,
                "signal": sig.model_dump(),
            },
        }
    )

    gate = get_risk_gate()
    risk = gate.check(built, sig, sz)
    await hub.publish(
        {
            "type": "risk",
            "payload": {
                "strategyId": strategy_id,
                "symbol": built.symbol,
                "t": built.ts,
                "risk": risk.model_dump(),
            },
        }
    )
    notional = abs(float(sz.target_notional))
    impact = None
    try:
        impact = float(
            built.liquidity.estimated_impact_bps(notional, side=side, pump=built.pump)
        )
    except (ValueError, TypeError, ZeroDivisionError):
        impact = None
    mint = built.meta.get("mint") if built.meta else None
    append_decision(
        make_row(
            ts=built.ts,
            strategy_id=strategy_id,
            symbol=built.symbol,
            mint=str(mint) if mint else None,
            stage="pre_order",
            outcome="reject" if not risk.allow else "emit_signal",
            signal=sig,
            risk=risk,
            notional_sol=notional,
            impact_bps_est=impact,
            impact_bps_cap=float(sz.max_slippage_bps),
        )
    )

    out: dict[str, Any] = {
        "signal": sig.model_dump(),
        "risk": risk.model_dump(),
        "fills": [],
        "ctx": {
            "symbol": built.symbol,
            "ts": built.ts,
            "tick": built.tick.model_dump() if built.tick else None,
            "liquidity": built.liquidity.model_dump(),
            "pump": built.pump.model_dump() if built.pump else None,
        },
    }

    if not risk.allow:
        gate.on_reject(built, risk.tags)
        reject = {"tags": risk.tags, "notes": risk.notes or ""}
        await hub.publish(
            {
                "type": "reject",
                "payload": {
                    "ts": built.ts,
                    "symbol": built.symbol,
                    "tags": reject["tags"],
                    "notes": reject["notes"],
                },
            }
        )
        out["reject"] = reject
        return out

    notional_out = float(risk.clipped_size) if risk.clipped_size is not None else abs(float(sz.target_notional))
    intent = OrderIntent(
        side=side,
        order_type="market",
        qty_or_notional=notional_out,
        max_slippage_bps=sz.max_slippage_bps,
        client_tag=strategy_id,
    )
    executed = await execute_paper_order(built, intent, risk, auto_post_fill=auto_post_fill)
    out.update(executed)
    return out
