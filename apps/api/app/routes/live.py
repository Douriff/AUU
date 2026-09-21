"""Live adapter REST — disabled by default; 403 unless the hard gate is armed.

POST /api/v1/live/orders is never a chain submit in this PR.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter
from pydantic import BaseModel

from app.live.broker import run_live_order, run_live_pre_order
from app.live.gate import evaluate, set_limits, try_set_armed, try_set_disabled
from app.models.contracts import OrderIntent, RiskOut, SignalOut, SizeIn, StrategyContext
from app.routes.envelope import err, ok

router = APIRouter(prefix="/api/v1/live", tags=["live"])


class LiveLimitsBody(BaseModel):
    max_notional_sol: Optional[float] = None
    max_day_loss: Optional[float] = None
    max_open_mints: Optional[int] = None


class LiveArmBody(BaseModel):
    armed: bool = False


class LiveDisabledBody(BaseModel):
    live_disabled: bool = True


class LiveOrderBody(BaseModel):
    ctx: StrategyContext
    intent: OrderIntent
    risk: Optional[RiskOut] = None
    signal: Optional[SignalOut] = None
    size: Optional[SizeIn] = None
    auto_post_fill: bool = True


def _blocked(status) -> Any:
    reasons = list(status.reasons)
    code = status.primary_reason()
    return err(
        code,
        "live refused: " + ",".join(reasons) if reasons else "live refused",
        403,
        extra={"reasons": reasons},
    )


@router.get("/status")
def live_status():
    return ok(evaluate().as_dict())


@router.get("/limits")
def live_limits():
    st = evaluate()
    data = st.limits.as_dict()
    data["missing"] = list(st.limits_missing)
    data["liveDisabled"] = st.live_disabled
    data["liveArmed"] = st.live_armed
    return ok(data)


@router.put("/limits")
def put_live_limits(body: LiveLimitsBody):
    """Save placeholder limits. Zero/null does not arm. Keypair is never uploaded."""
    dumped = body.model_dump(exclude_unset=True)
    st = set_limits(
        max_notional_sol=dumped.get("max_notional_sol"),
        max_day_loss=dumped.get("max_day_loss"),
        max_open_mints=dumped.get("max_open_mints"),
        present=set(dumped.keys()),
    )
    return ok(st.as_dict())


@router.put("/disabled")
def put_live_disabled(body: LiveDisabledBody):
    """LiveDisabled switch. Turning it off requires keypair + all three limits."""
    ok_set, st = try_set_disabled(body.live_disabled)
    if not ok_set:
        return _blocked(st)
    return ok(st.as_dict())


@router.put("/arm")
def put_live_arm(body: LiveArmBody):
    """Explicit arm. Refuses unless switch is off, keypair exists, and limits are set."""
    ok_set, st = try_set_armed(body.armed)
    if not ok_set:
        return _blocked(st)
    return ok(st.as_dict())


@router.post("/pre-order")
async def live_pre_order(body: LiveOrderBody):
    status = evaluate()
    if not status.armed:
        return _blocked(status)
    signal = body.signal or SignalOut(side="long" if body.intent.side == "buy" else "short")
    size = body.size or SizeIn(
        target_notional=abs(float(body.intent.qty_or_notional)),
        max_slippage_bps=body.intent.max_slippage_bps,
    )
    risk = await run_live_pre_order(body.ctx, signal, size)
    return ok({**risk.model_dump(), "venue": "live"})


@router.post("/orders")
async def live_orders(body: LiveOrderBody):
    status = evaluate()
    if not status.armed:
        return _blocked(status)
    signal = body.signal or SignalOut(side="long" if body.intent.side == "buy" else "short")
    size = body.size or SizeIn(
        target_notional=abs(float(body.intent.qty_or_notional)),
        max_slippage_bps=body.intent.max_slippage_bps,
    )
    risk = body.risk
    if risk is None:
        risk = await run_live_pre_order(body.ctx, signal, size)
    if not risk.allow:
        return ok(
            {
                "fills": [],
                "reject": {"tags": risk.tags, "notes": risk.notes or ""},
                "venue": "live",
                "risk": risk.model_dump(),
            }
        )
    data = await run_live_order(body.ctx, body.intent, risk, auto_post_fill=body.auto_post_fill)
    data["risk"] = risk.model_dump()
    return ok(data)
