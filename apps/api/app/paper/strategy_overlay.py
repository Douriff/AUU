"""Read-only chart overlay for the M3 strategy (report P1-5).

For one coin: the paper runner's rebalance fills (markers) and, per day, the TSMOM look-back
returns, the signal and the target weight, computed by the strategy's own ``targets()`` on the
same store panel the runner reads, next to the targets the runner actually recorded for each
day (``runs.targets``). The two differ when the data under a past day changed after it ran (late
backfill, a coin added to the store): those days are listed in ``revised`` so the difference is
visible instead of silently redrawn. Nothing here writes to the ledger or changes a decision; it
only re-reads what the runner already uses, so a person can check "why did it add/cut here".
"""
from __future__ import annotations

import json
import threading
import time
from typing import Any, Callable, Optional

from app.strategies.indicators import pct_change, sign

DAY_MS = 86_400_000
MAX_DAYS = 1000
_cache: dict[tuple, tuple[float, dict]] = {}
_clock = threading.Lock()
TTL_SEC = 600


def build(runner, symbol: str, *, panel_fn: Optional[Callable[[int], Any]] = None, days: int = 400) -> dict:
    sym = "".join(ch for ch in symbol.split("/")[0].upper() if ch.isalnum())
    days = max(30, min(int(days), MAX_DAYS))
    due = runner.due_day()
    key = (sym, due, days, id(runner))
    with _clock:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < TTL_SEC:
            return hit[1]
    strat = runner.strategy
    lbs = list(strat.params.lookbacks)
    panel = (panel_fn or runner.panel_fn)(due)
    series: list[dict] = []
    in_universe = bool(panel is not None and sym in panel.coins and sym in strat.coins(panel))
    if in_universe:
        close = panel.spot_close[sym]
        moms = [pct_change(close, L) for L in lbs]
        weights = strat.targets(panel)[sym]
        for t in range(max(0, len(panel) - days), len(panel)):
            ms = [m[t] for m in moms]
            sig = None
            if all(m is not None for m in ms):
                s = sum(sign(m) for m in ms) / len(ms)
                sig = max(0.0, s) if strat.params.long_only else s
            series.append({"ts": int(panel.days[t]), "close": close[t], "mom": ms, "signal": sig, "target": weights[t]})
    recorded: dict[int, float] = {}
    for r in runner.ledger.runs():
        try:
            tg = json.loads(r["targets"] or "{}")
        except (TypeError, ValueError):
            continue
        recorded[int(r["day"])] = float(tg.get(sym, 0.0) or 0.0)
    revised = []
    for row in series:
        rec = recorded.get(row["ts"])
        row["recorded"] = rec
        if rec is not None and abs(rec - row["target"]) > 1e-9:
            revised.append(row["ts"])
    fills = []
    for r in runner.ledger.fills(limit=5000):
        if r["coin"] != sym:
            continue
        fills.append({"day": int(r["day"]), "side": r["side"], "wFrom": r["w_from"], "wTo": r["w_to"],
                      "price": r["price"], "fillPrice": r["fill_price"], "notional": r["notional"]})
    fills.sort(key=lambda f: f["day"])
    out = {"symbol": sym, "strategy": strat.name, "lookbacks": lbs, "longOnly": strat.params.long_only,
           "asOfDay": due, "inUniverse": in_universe, "series": series, "fills": fills,
           "recordedDays": len(recorded), "revised": revised}
    with _clock:
        if len(_cache) > 64:
            _cache.clear()
        _cache[key] = (time.time(), out)
    return out


def clear_cache() -> None:
    with _clock:
        _cache.clear()
