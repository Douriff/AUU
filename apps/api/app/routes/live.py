"""Live adapter REST — disabled by default; 403 unless the hard gate is armed.

POST /api/v1/live/orders is never a chain submit in this PR.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter
from pydantic import BaseModel

from app.live.broker import run_live_order, run_live_pre_order
from app.live.gate import (
    REASON_LIVE_DISABLED,
    evaluate,
    set_limits,
    try_set_armed,
    try_set_disabled,
    try_set_enabled_with_confirm,
)
from app.live.ledger import build_live_ledger
from app.models.contracts import OrderIntent, RiskOut, SignalOut, SizeIn, StrategyContext
from app.routes.envelope import err, ok

router = APIRouter(prefix="/api/v1/live", tags=["live"])


class LiveLimitsBody(BaseModel):
    max_notional_sol: Optional[float] = None
    max_day_loss_pct: Optional[float] = None
    max_open_mints: Optional[int] = None


class LiveArmBody(BaseModel):
    armed: bool = False


class LiveDisabledBody(BaseModel):
    live_disabled: bool = True


class LiveEnabledBody(BaseModel):
    liveEnabled: bool = False
    confirmed: bool = False
    live_enabled: Optional[bool] = None
    live_confirmed: Optional[bool] = None


class LiveOrderBody(BaseModel):
    ctx: StrategyContext
    intent: OrderIntent
    risk: Optional[RiskOut] = None
    signal: Optional[SignalOut] = None
    size: Optional[SizeIn] = None
    auto_post_fill: bool = True


def _blocked(status, risk: Optional[RiskOut] = None) -> Any:
    reasons = list(status.reasons)
    if REASON_LIVE_DISABLED not in reasons:
        reasons = [REASON_LIVE_DISABLED] + reasons
    code = status.primary_reason() if status.reasons else REASON_LIVE_DISABLED
    extra: dict[str, Any] = {"reasons": reasons, "tags": list(reasons)}
    if risk is not None:
        extra["risk"] = risk.model_dump()
        extra["tags"] = list(risk.tags)
    return err(
        code,
        "live refused: " + ",".join(reasons) if reasons else "live refused",
        403,
        extra=extra,
    )


@router.get("/status")
def live_status():
    return ok(evaluate().as_dict())


@router.get("/limits")
def live_limits():
    st = evaluate()
    data = st.limits.as_dict()
    data["missing"] = list(st.limits_missing)
    data["liveEnabled"] = st.live_enabled
    data["liveConfirmed"] = st.live_confirmed
    data["liveDisabled"] = st.live_disabled
    data["liveArmed"] = st.live_armed
    data["keypairMounted"] = "yes" if st.keypair_configured else "no"
    return ok(data)


@router.get("/ledger")
def live_ledger():
    """source=live fills only. Empty in this PR; never mixed into paper win-rate."""
    return ok(build_live_ledger())


@router.put("/limits")
def put_live_limits(body: LiveLimitsBody):
    """Limits are locked. Body is ignored; always returns authorized caps."""
    st = set_limits(
        max_notional_sol=body.max_notional_sol,
        max_day_loss_pct=body.max_day_loss_pct,
        max_open_mints=body.max_open_mints,
    )
    return ok(st.as_dict())


@router.put("/disabled")
def put_live_disabled(body: LiveDisabledBody):
    """LiveDisabled switch. Turning it off requires a local keypair (limits are locked)."""
    ok_set, st = try_set_disabled(body.live_disabled)
    if not ok_set:
        return _blocked(st)
    return ok(st.as_dict())


@router.put("/enabled")
def put_live_enabled(body: LiveEnabledBody):
    """liveEnabled + secondary confirm. Enabling without confirmed=true stays LIVE_DISABLED."""
    want = body.live_enabled if body.live_enabled is not None else body.liveEnabled
    confirmed = body.live_confirmed if body.live_confirmed is not None else body.confirmed
    ok_set, st = try_set_enabled_with_confirm(bool(want), confirmed=bool(confirmed))
    if not ok_set:
        return _blocked(st)
    return ok(st.as_dict())


@router.put("/arm")
def put_live_arm(body: LiveArmBody):
    """Explicit arm. Refuses unless switch is off and a local keypair exists."""
    ok_set, st = try_set_armed(body.armed)
    if not ok_set:
        return _blocked(st)
    return ok(st.as_dict())


@router.post("/pre-order")
async def live_pre_order(body: LiveOrderBody):
    status = evaluate()
    signal = body.signal or SignalOut(side="long" if body.intent.side == "buy" else "short")
    size = body.size or SizeIn(
        target_notional=abs(float(body.intent.qty_or_notional)),
        max_slippage_bps=body.intent.max_slippage_bps,
    )
    risk = await run_live_pre_order(body.ctx, signal, size)
    if not status.armed:
        return _blocked(status, risk=risk)
    return ok({**risk.model_dump(), "venue": "live"})


@router.post("/orders")
async def live_orders(body: LiveOrderBody):
    status = evaluate()
    signal = body.signal or SignalOut(side="long" if body.intent.side == "buy" else "short")
    size = body.size or SizeIn(
        target_notional=abs(float(body.intent.qty_or_notional)),
        max_slippage_bps=body.intent.max_slippage_bps,
    )
    risk = body.risk
    if risk is None:
        risk = await run_live_pre_order(body.ctx, signal, size)
    if not status.armed:
        return _blocked(status, risk=risk)
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
