"""M3 strategy console API (read-only). Login required when AUU_AUTH is on."""
from __future__ import annotations

from fastapi import APIRouter, Query, Request

from app.routes.envelope import err, ok
from app.routes.mainstream_paper import _uid

router = APIRouter(prefix="/api/v1/mainstream/strategy", tags=["mainstream-strategy"])


@router.get("")
def strategy_summary(request: Request):
    # The auth gate already answers 401; checked again here so the route never leaks without it.
    if _uid(request) is None:
        return err("AUTH_REQUIRED", "请先登录", 401)
    from app.paper.strategy_runner import get_runner

    return ok(get_runner().summary())


@router.get("/report")
def strategy_report(request: Request):
    """Performance report page (monthly heatmap, drawdown, Sortino/Calmar, per-coin attribution)."""
    if _uid(request) is None:
        return err("AUTH_REQUIRED", "请先登录", 401)
    from app.paper import perf_report
    from app.paper.strategy_runner import _store_funding, get_runner

    return ok(perf_report.build(get_runner(), funding_fn=_store_funding))


@router.get("/overlay")
def strategy_overlay(request: Request, symbol: str = Query(..., min_length=1, max_length=20), days: int = Query(400, ge=30, le=1000)):
    """Chart overlay (read-only): rebalance fills plus look-back returns / signal / target weight for one coin."""
    if _uid(request) is None:
        return err("AUTH_REQUIRED", "请先登录", 401)
    from app.paper import strategy_overlay
    from app.paper.strategy_runner import get_runner

    return ok(strategy_overlay.build(get_runner(), symbol, days=days))
