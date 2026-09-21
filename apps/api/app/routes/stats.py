"""GET /api/v1/stats/paper-performance — alias of strategy pump-paper-v1/stats."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Query

from app.paper.ledger import build_performance
from app.routes.envelope import ok

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
