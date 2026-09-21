"""GET/PUT /api/v1/strategy/pump-paper-v1 — paper strategy params + monitor."""
from __future__ import annotations

import time
from typing import Any, Optional

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from app.bus import get_hub
from app.live.gate import evaluate
from app.paper.ledger import build_performance, reset_paper_journal
from app.paper.decision_log import get_decision_log, reset_decision_log
from app.risk import get_risk_gate
from app.routes.envelope import err, ok
from app.strategies.pump_paper_v1 import STRATEGY_ID, get_engine
from app.traders import COPY_TRADE_ENABLED
from app.traders.distill import DistillReject, apply_distill

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
    min_trade_count_1m: Optional[int] = Field(default=None, ge=0)
    min_buy_sell_ratio_1m: Optional[float] = Field(default=None, ge=0)


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
        "liveDisabled": evaluate().live_disabled,
        "copy_trade_enabled": COPY_TRADE_ENABLED,
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


@router.get("/pump-paper-v1/decision-log")
def get_decision_log_route(
    from_ts: Optional[int] = Query(None, alias="from"),
    to_ts: Optional[int] = Query(None, alias="to"),
    limit: int = Query(200, ge=1, le=2000),
):
    """Paper DecisionLog rows. No secrets, no chain send."""
    rows = get_decision_log().query(from_ts=from_ts, to_ts=to_ts, limit=max(1, min(limit, 2000)))
    return ok(
        {
            "items": [r.model_dump() for r in rows],
            "n": len(rows),
            "liveEnabled": False,
            "liveDisabled": True,
        }
    )


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
    reset_decision_log()
    get_engine().reset_eval_counts()
    return ok(build_performance())


class ApplyDistillBody(BaseModel):
    confirm: bool = False
    source_watch_id: str
    suggested_params: Optional[dict[str, Any]] = None


@router.post("/pump-paper-v1/apply-distill")
def post_apply_distill(body: ApplyDistillBody):
    """Write distilled paper params. Requires confirm=true. Never toggles auto_paper_orders."""
    try:
        applied = apply_distill(
            confirm=body.confirm,
            source_watch_id=body.source_watch_id,
            suggested_params=body.suggested_params,
        )
    except DistillReject as e:
        status = 404 if e.code == "NOT_FOUND" else 400
        return err(e.code, e.message, status)
    data = _state_payload()
    data["distill"] = applied
    return ok(data)
