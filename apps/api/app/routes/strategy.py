"""GET/PUT /api/v1/strategy/pump-paper-v1 — paper strategy params + monitor."""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter
from pydantic import BaseModel

from app.risk import get_risk_gate
from app.routes.envelope import ok
from app.strategies.pump_paper_v1 import STRATEGY_ID, get_engine

router = APIRouter(prefix="/api/v1/strategy", tags=["strategy"])


class PumpPaperParamsPatch(BaseModel):
    progress_bps_min: Optional[int] = None
    progress_bps_max: Optional[int] = None
    max_impact_bps: Optional[float] = None
    take_profit_pct: Optional[float] = None
    stop_loss_pct: Optional[float] = None
    max_hold_sec: Optional[int] = None
    cooldown_sec: Optional[int] = None
    max_day_loss_pct: Optional[float] = None
    max_open_mints: Optional[int] = None
    notional_pct_equity: Optional[float] = None
    auto_paper_orders: Optional[bool] = None
    max_notional_sol: Optional[float] = None


def _state_payload() -> dict[str, Any]:
    engine = get_engine()
    gate = get_risk_gate()
    positions = [
        {
            "symbol": p.symbol,
            "mint": p.mint,
            "qty": p.qty,
            "entry_price": p.entry_price,
            "entry_ts": p.entry_ts,
            "entry_notional": p.entry_notional,
        }
        for p in engine.positions.values()
    ]
    return {
        "strategyId": STRATEGY_ID,
        "params": engine.params.model_dump(),
        "auto_paper_orders": engine.params.auto_paper_orders,
        "trading_state": gate.trading_state,
        "day_pnl": gate.day_pnl,
        "positions": positions,
    }


@router.get("/pump-paper-v1")
def get_pump_paper():
    return ok(_state_payload())


@router.put("/pump-paper-v1")
@router.post("/pump-paper-v1")
def put_pump_paper(body: PumpPaperParamsPatch):
    engine = get_engine()
    engine.update_params(body.model_dump(exclude_none=True))
    return ok(_state_payload())


@router.get("/pump-paper-v1/monitor")
def get_monitor():
    return ok(get_engine().monitor_rows())
