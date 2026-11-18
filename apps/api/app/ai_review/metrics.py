"""Performance math over rows that were already stored.

Unknown inputs stay null. A zero is emitted only when the stored series
itself is zero. Signal-row counts never become trade samples.
"""
from __future__ import annotations

from typing import Any, Optional

from app.ai_review.schema import CHAIN_ABS, DAY_MS, SHORT_NAV, SHORT_TRADES, STALE_GRACE_MS


def _blank(status: str, reason: Optional[str], *, book: str, basis: Optional[str] = None) -> dict[str, Any]:
    return {
        "status": status,
        "reason": reason,
        "book": book,
        "basis": basis,
        "principal": None,
        "principal_ccy": None,
        "capital_kind": None,
        "window": None,
        "total_return": None,
        "max_drawdown": None,
        "cost": None,
        "cost_unit": None,
        "cost_status": "unknown",
        "closed_trades": None,
        "open_trades": None,
        "nav_points": None,
        "signal_rows": None,
        "as_of_ts": None,
        "recorded_at": None,
        "stale": None,
        "warnings": [],
        "method": None,
        "curve": [],
        "irregular": None,
    }


def _window(points: list[dict], tf_ms: int) -> dict[str, Any]:
    ts = [int(p["ts"]) for p in points]
    gaps = [b - a for a, b in zip(ts, ts[1:])]
    return {
        "start_ts": ts[0],
        "end_ts": ts[-1],
        "tf_ms": tf_ms,
        "n": len(ts),
        "ts": ts,
        "irregular": any(gap != tf_ms for gap in gaps) if gaps else False,
    }


def _drawdown(path: list[float]) -> Optional[float]:
    if not path or any(point <= 0 for point in path):
        return None
    peak = path[0]
    worst = 0.0
    for point in path:
        peak = max(peak, point)
        worst = min(worst, point / peak - 1.0)
    return worst


def _chain_from(start: float, points: list[dict]) -> bool:
    nav = start
    for point in points:
        ret = point.get("ret")
        level = point.get("nav")
        if ret is None or level is None:
            return False
        expect = nav * (1.0 + float(ret))
        if abs(expect - float(level)) > CHAIN_ABS * max(1.0, abs(float(level))):
            return False
        nav = float(level)
    return True


def _stale(last_ts: Optional[int], now_ms: int, tf_ms: int) -> Optional[bool]:
    if last_ts is None:
        return None
    return (now_ms - int(last_ts)) > (tf_ms + STALE_GRACE_MS)


def _curve(path_ts: list[tuple[int, float]]) -> list[dict[str, float]]:
    from app.ai_review.schema import MAX_CURVE

    if len(path_ts) <= MAX_CURVE:
        return [{"ts": ts, "nav": nav} for ts, nav in path_ts]
    step = len(path_ts) / MAX_CURVE
    picked = [path_ts[min(len(path_ts) - 1, int(i * step))] for i in range(MAX_CURVE)]
    if picked[-1] != path_ts[-1]:
        picked[-1] = path_ts[-1]
    return [{"ts": ts, "nav": nav} for ts, nav in picked]


def from_unit_nav(
    points: list[dict],
    *,
    now_ms: int,
    tf_ms: int,
    book: str,
    start: float = 1.0,
    basis: str = "unit_nav",
) -> dict[str, Any]:
    """Compound a stored NAV that is defined against an implicit starting unit."""
    if not points:
        return _blank("empty", "no_nav_rows", book=book, basis=basis)
    ordered = sorted(points, key=lambda row: int(row["ts"]))
    if len({int(row["ts"]) for row in ordered}) != len(ordered):
        out = _blank("inconsistent", "duplicate_ts", book=book, basis=basis)
        out["nav_points"] = len(ordered)
        return out
    if any(row.get("ret") is None or row.get("nav") is None for row in ordered):
        return _blank("non_finite", "nav_or_return_missing", book=book, basis=basis)
    if not _chain_from(start, ordered):
        out = _blank("inconsistent", "nav_return_mismatch", book=book, basis=basis)
        out["nav_points"] = len(ordered)
        return out
    window = _window(ordered, tf_ms)
    path = [start] + [float(row["nav"]) for row in ordered]
    last = ordered[-1]
    recorded = [row["recorded_at"] for row in ordered if row.get("recorded_at") is not None]
    warnings = []
    if len(ordered) < SHORT_NAV:
        warnings.append("short_nav")
    if window["irregular"]:
        warnings.append("irregular_spacing")
    stale = _stale(int(last["ts"]), now_ms, tf_ms)
    if stale:
        warnings.append("stale")
    total = float(last["nav"]) / float(start) - 1.0
    return {
        "status": "ok",
        "reason": None,
        "book": book,
        "basis": basis,
        "principal": None,
        "principal_ccy": None,
        "capital_kind": "accounting_unit" if start != 1.0 else "unit_nav",
        "accounting_unit": start,
        "window": {key: window[key] for key in ("start_ts", "end_ts", "tf_ms", "n")},
        "ts": window["ts"],
        "total_return": total,
        "max_drawdown": _drawdown(path),
        "cost": None,
        "cost_unit": None,
        "cost_status": "unknown",
        "closed_trades": None,
        "open_trades": None,
        "nav_points": len(ordered),
        "signal_rows": None,
        "as_of_ts": int(last["ts"]),
        "recorded_at": max(recorded) if recorded else None,
        "stale": stale,
        "warnings": warnings,
        "method": "compounded_from_stored_nav",
        "curve": _curve([(int(row["ts"]), float(row["nav"])) for row in ordered]),
        "irregular": window["irregular"],
    }


def attach_trade_costs(perf: dict[str, Any], trades: list[dict], *, cost_per_side: float) -> dict[str, Any]:
    """Fill cost only when every closed trade matches the registered round-trip identity."""
    if perf.get("status") not in {"ok", "empty"}:
        return perf
    closed = [row for row in trades if row.get("exit_ts") is not None]
    opened = [row for row in trades if row.get("exit_ts") is None]
    perf["closed_trades"] = len(closed)
    perf["open_trades"] = len(opened)
    if len(closed) < SHORT_TRADES:
        perf.setdefault("warnings", []).append("short_trades")
    if not closed:
        perf["cost_status"] = "unknown"
        perf["cost"] = None
        return perf
    total = 0.0
    for row in closed:
        checked = _round_trip_cost(row, cost_per_side)
        if checked is None:
            perf["cost"] = None
            perf["cost_status"] = "inconsistent"
            perf["cost_unit"] = None
            return perf
        total += checked
    perf["cost"] = total
    perf["cost_status"] = "recorded"
    perf["cost_unit"] = "fraction_of_notional_sum"
    return perf


def _round_trip_cost(row: dict, cost_per_side: float) -> Optional[float]:
    gross = row.get("gross")
    funding = row.get("funding")
    net = row.get("net")
    entry = row.get("entry_px")
    exit_px = row.get("exit_px")
    side = row.get("side")
    if None in (gross, funding, net, entry, exit_px) or side not in {"long", "short"}:
        return None
    if entry <= 0 or exit_px <= 0:
        return None
    expected_gross = (exit_px / entry - 1.0) if side == "long" else (1.0 - exit_px / entry)
    if abs(expected_gross - float(gross)) > 1e-6:
        return None
    funding_component = float(funding) if side == "long" else -float(funding)
    implied = float(gross) - funding_component - float(net)
    expected = 2.0 * float(cost_per_side)
    if abs(implied - expected) > 1e-8:
        return None
    return expected


def paper_performance(points: list[dict], *, start_nav: Optional[float], now_ms: int) -> dict[str, Any]:
    perf = from_unit_nav(points, now_ms=now_ms, tf_ms=DAY_MS, book="paper", start=start_nav or 1.0, basis="paper_nav")
    if start_nav is None:
        # Do not borrow the process default as if it were the recorded book.
        if perf["status"] == "ok":
            perf["status"] = "inconsistent"
            perf["reason"] = "principal_not_recorded"
            perf["total_return"] = None
            perf["max_drawdown"] = None
            perf["curve"] = []
        perf["principal"] = None
        return perf
    if perf["status"] != "ok":
        perf["principal"] = start_nav
        perf["principal_ccy"] = "USDT"
        perf["capital_kind"] = "paper_simulation"
        return perf
    costs = [row.get("cost") for row in points]
    if any(cost is None for cost in costs):
        perf["cost"] = None
        perf["cost_status"] = "unknown"
    else:
        perf["cost"] = sum(float(cost) for cost in costs)
        perf["cost_status"] = "recorded"
        perf["cost_unit"] = "USDT"
    perf["principal"] = start_nav
    perf["principal_ccy"] = "USDT"
    perf["capital_kind"] = "paper_simulation"
    perf["closed_trades"] = None
    return perf


def sum_recorded_costs(points: list[dict]) -> tuple[Optional[float], str]:
    costs = [row.get("cost") for row in points]
    if not points or any(cost is None for cost in costs):
        return None, "unknown"
    return sum(float(cost) for cost in costs), "recorded"


def comparable(left: dict[str, Any], right: dict[str, Any]) -> tuple[bool, str]:
    """Same book, basis, and timestamp list. Otherwise refuse."""
    if left.get("status") != "ok" or right.get("status") != "ok":
        return False, "missing_performance"
    if left.get("total_return") is None or right.get("total_return") is None:
        return False, "return_missing"
    if left.get("max_drawdown") is None or right.get("max_drawdown") is None:
        return False, "drawdown_missing"
    if left.get("book") != right.get("book") or left.get("basis") != right.get("basis"):
        return False, "basis_mismatch"
    if left.get("ts") != right.get("ts"):
        return False, "window_mismatch"
    window = left.get("window") or {}
    other = right.get("window") or {}
    if window.get("tf_ms") != other.get("tf_ms"):
        return False, "window_mismatch"
    return True, "same_window"
