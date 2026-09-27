"""GET /api/v1/stats/paper-performance — alias of strategy pump-paper-v1/stats.

GET /api/v1/stats/executability — paper→live evidence (live stays disabled).
GET /api/v1/stats/postmortem — alias of strategy pump-paper-v1/postmortem.
GET /api/v1/stats/exec-report — ExecReport field pack only.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Query

from app.paper.executability import build_executability
from app.paper.ledger import build_performance
from app.paper.postmortem import ScenarioProgressForbidden, build_exec_report_endpoint, build_postmortem
from app.routes.envelope import err, ok

router = APIRouter(prefix="/api/v1/stats", tags=["stats"])


def _mc_flag(mc: str | bool) -> bool:
    if isinstance(mc, bool):
        return mc
    return str(mc).strip().lower() in {"1", "true", "yes", "on"}


@router.get("/paper-performance")
def paper_performance(
    window: str = Query("session", description="session or last N closed trades (int)"),
    from_ts: Optional[int] = Query(None, alias="from"),
    to_ts: Optional[int] = Query(None, alias="to"),
    n_paths: int = Query(500, ge=1, le=20_000),
    seed: int = Query(42),
    mc: str = Query("0", description="1 to run trades-MC; default off"),
    method: str = Query("shuffle", description="shuffle | bootstrap"),
):
    mc_method = "bootstrap" if method in {"resample", "bootstrap"} else "shuffle"
    data = build_performance(
        window=window,
        from_ts=from_ts,
        to_ts=to_ts,
        n_paths=n_paths,
        seed=seed,
        mc=_mc_flag(mc),
        mc_method=mc_method,
    )
    return ok(data)


@router.get("/executability")
def executability(
    window: str = Query("session", description="session or last N closed trades (int)"),
    from_ts: Optional[int] = Query(None, alias="from"),
    to_ts: Optional[int] = Query(None, alias="to"),
):
    """Paper journal + strategy eval aggregations. Never enables live or sends txs."""
    data = build_executability(window=window, from_ts=from_ts, to_ts=to_ts)
    return ok(data)


@router.get("/postmortem")
def postmortem_alias(
    window: str = Query("session", description="session | last_n | int"),
    n: int = Query(30, ge=1, le=5_000),
    from_ts: Optional[int] = Query(None, alias="from"),
    to_ts: Optional[int] = Query(None, alias="to"),
    rolling: int = Query(10, ge=1, le=500),
    scenario: str = Query("off", description="off | momentum_delta"),
    min_trade_count_1m: Optional[int] = Query(None, ge=0),
    min_buy_sell_ratio_1m: Optional[float] = Query(None, ge=0),
    progress_bps_min: Optional[int] = Query(None),
    progress_bps_max: Optional[int] = Query(None),
    setup_seed_tags: Optional[str] = Query(None),
):
    """Alias of GET /api/v1/strategy/pump-paper-v1/postmortem."""
    seeds = [s.strip() for s in str(setup_seed_tags).split(",") if s.strip()] if setup_seed_tags else None
    try:
        data = build_postmortem(
            window=window,
            n=n,
            from_ts=from_ts,
            to_ts=to_ts,
            rolling=rolling,
            scenario_tag=scenario,
            min_trade_count_1m=min_trade_count_1m,
            min_buy_sell_ratio_1m=min_buy_sell_ratio_1m,
            progress_bps_min=progress_bps_min,
            progress_bps_max=progress_bps_max,
            setup_seed_tags=seeds,
        )
    except ScenarioProgressForbidden as e:
        return err(e.code, str(e), 400, extra={"forbidden_touched": e.touched})
    return ok(data)


@router.get("/exec-report")
def exec_report(
    window: str = Query("session", description="session | last_n | int"),
    n: int = Query(30, ge=1, le=5_000),
    from_ts: Optional[int] = Query(None, alias="from"),
    to_ts: Optional[int] = Query(None, alias="to"),
):
    """ExecReport field pack only (also nested under postmortem.exec)."""
    return ok(build_exec_report_endpoint(window=window, n=n, from_ts=from_ts, to_ts=to_ts))
