"""GET/PUT /api/v1/strategy/pump-paper-v1 — paper strategy params + monitor."""
from __future__ import annotations

import time
from typing import Any, Optional

from fastapi import APIRouter
from pydantic import BaseModel

from app.bus import get_hub
from app.paper.ledger import build_performance, reset_paper_journal
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
    strategy_autopaper: Optional[bool] = None
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
        "strategy_autopaper": engine.params.auto_paper_orders,
        "trading_state": gate.trading_state,
        "day_pnl": gate.day_pnl,
        "positions": positions,
        "last_decisions": engine.last_decisions(20),
        "liveDisabled": True,
    }


@router.get("/pump-paper-v1")
def get_pump_paper():
    return ok(_state_payload())


@router.put("/pump-paper-v1")
@router.post("/pump-paper-v1")
def put_pump_paper(body: PumpPaperParamsPatch):
    engine = get_engine()
    patch = body.model_dump(exclude_none=True)
    if "strategy_autopaper" in patch:
        patch["auto_paper_orders"] = patch.pop("strategy_autopaper")
    prev_auto = bool(engine.params.auto_paper_orders)
    engine.update_params(patch)
    if "auto_paper_orders" in patch and bool(engine.params.auto_paper_orders) != prev_auto:
        gate = get_risk_gate()
        auto = bool(engine.params.auto_paper_orders)
        get_hub().publish_sync(
            {
                "type": "trading_state",
                "payload": {
                    "state": gate.trading_state,
                    "reason": "autopaper_on" if auto else "autopaper_off",
                    "symbol": "",
                    "ts": int(time.time() * 1000),
                    "auto_paper_orders": auto,
                    "strategy_autopaper": auto,
                },
            }
        )
    return ok(_state_payload())


@router.get("/pump-paper-v1/monitor")
def get_monitor():
    return ok(get_engine().monitor_rows())


@router.get("/pump-paper-v1/stats")
def get_pump_paper_stats(
    window: str = "session",
    mc: str = "0",
    n_paths: int = 500,
    seed: int = 42,
    method: str = "shuffle",
):
    """PaperTradeJournal PaperStats. MC off unless mc=1. Win rate from round-trips only."""
    enabled = str(mc).strip().lower() in {"1", "true", "yes", "on"}
    if method in {"resample", "bootstrap"}:
        mc_method = "bootstrap"
    else:
        mc_method = "shuffle"
    data = build_performance(
        window=window,
        n_paths=n_paths,
        seed=seed,
        mc=enabled,
        mc_method=mc_method,
    )
    return ok(data)


@router.post("/pump-paper-v1/stats/reset")
def reset_pump_paper_stats():
    reset_paper_journal()
    return ok(build_performance())
