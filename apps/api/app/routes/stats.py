"""GET /api/v1/stats/paper-performance — paper session stats + Monte Carlo sim."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Query

from app.paper.ledger import get_paper_ledger, summarize
from app.routes.envelope import ok
from app.strategies.pump_paper_v1 import get_engine

router = APIRouter(prefix="/api/v1/stats", tags=["stats"])


@router.get("/paper-performance")
def paper_performance(
    window: str = Query("session", description="session or last N closed trades (int)"),
    from_ts: Optional[int] = Query(None, alias="from"),
    to_ts: Optional[int] = Query(None, alias="to"),
    n_paths: int = Query(1000, ge=1, le=20_000),
    seed: int = Query(42),
):
    engine = get_engine()
    n_window: Optional[int] = None
    window_label: str | int = "session"
    raw = str(window).strip().lower()
    if raw not in {"", "session", "all"}:
        try:
            n_window = max(1, int(raw))
            window_label = n_window
        except ValueError:
            window_label = "session"
    ledger = get_paper_ledger()
    trades = ledger.closed_in_window(window=n_window, from_ts=from_ts, to_ts=to_ts)
    data = summarize(
        trades,
        stop_loss_pct=float(engine.params.stop_loss_pct),
        day_loss_pct=float(engine.params.max_day_loss_pct),
        n_paths=n_paths,
        seed=seed,
        window=window_label,
    )
    data["open_lots"] = sum(len(v) for v in ledger.lots.values())
    data["fill_count"] = len(ledger.fills)
    data["auto_paper_orders"] = engine.params.auto_paper_orders
    data["strategy_autopaper"] = engine.params.auto_paper_orders
    data["strategyId"] = "pump-paper-v1"
    return ok(data)
