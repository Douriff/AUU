"""CompareReport stub — reference_only; win rate still from own paper journal."""
from __future__ import annotations

import time
from typing import Any, Optional

from app.models.contracts import CompareReport
from app.paper.ledger import build_performance
from app.traders.habits import habit_profile
from app.traders.snapshot import get_snapshot
from app.traders.store import get_watch


def compare_report(
    watch_id_or_address: str,
    *,
    from_ts: Optional[int] = None,
    to_ts: Optional[int] = None,
) -> Optional[CompareReport]:
    item = get_watch(watch_id_or_address)
    if item is None:
        return None
    now = int(time.time() * 1000)
    window = {"from_ts": from_ts, "to_ts": to_ts or now}
    self_stats = build_performance()
    snap = get_snapshot(item.watch_id)
    profile = habit_profile(item.watch_id)
    n_trades = 0
    approx_pnl = 0.0
    if snap is not None:
        n_trades = len(snap.recent_buys) + len(snap.recent_sells)
        approx_pnl = float(snap.gross_exposure_sol)
        for pos in snap.positions:
            approx_pnl += float(pos.unrealized_pnl_sol or 0.0)
    tags_hist = [t.tag for t in profile.tags] if profile else []
    trader_ref: dict[str, Any] = {
        "n_trades": n_trades,
        "approx_pnl": round(approx_pnl, 6),
        "tags_hist": tags_hist,
        "reference_only": True,
    }
    return CompareReport(
        window=window,
        self=self_stats,
        trader_ref=trader_ref,
        note="reference_only — not copy-trading",
    )
