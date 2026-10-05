"""Public status page data (P1-6): coarse, login-free system health plus a 30-day availability record.

The API records one row per minute while it runs (``uptime.sqlite`` in the data dir):
  up       the process was alive and answering (the row exists)
  healthy  the same checks auu-guard asserts: live stays locked, market data fresh, strategy not overdue
Minutes with no row are downtime (process stopped / host down). Availability is measured from the
first recorded minute (or the 30-day window start, whichever is later), so a new install does not
claim 30 days it never measured.

The public payload is deliberately coarse: no positions, NAV, prices, user data, internal paths,
versions, hostnames or exchange error strings.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from app.data_paths import data_dir

log = logging.getLogger(__name__)

MIN = 60_000
DAY = 86_400_000
KEEP_DAYS = 90
_SCHEMA = """
CREATE TABLE IF NOT EXISTS minutes (ts INTEGER PRIMARY KEY, healthy INTEGER NOT NULL, data_fresh INTEGER NOT NULL,
                                    strategy_ok INTEGER NOT NULL, live_locked INTEGER NOT NULL);
"""


def uptime_enabled() -> bool:
    from app.role import is_standby

    if is_standby():
        return False
    return os.getenv("AUU_UPTIME", "on").strip().lower() not in {"0", "false", "off", "no"}


def _bj_day(ms: int) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime((ms + 8 * 3_600_000) / 1000))


class Uptime:
    def __init__(self, path: Optional[Path] = None, *, now_ms: Callable[[], int] = lambda: int(time.time() * 1000)):
        self.path = Path(path) if path else data_dir() / "uptime.sqlite"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.now_ms = now_ms
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(self.path), check_same_thread=False)
        with self._lock:
            self._db.executescript(_SCHEMA)
            self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def record(self, checks: dict, ts: Optional[int] = None) -> dict:
        """Write the current minute. ``checks``: data_fresh, strategy_ok, live_locked (bools)."""
        t = (self.now_ms() if ts is None else ts) // MIN * MIN
        f, s, l = bool(checks.get("data_fresh")), bool(checks.get("strategy_ok")), bool(checks.get("live_locked"))
        row = (t, int(f and s and l), int(f), int(s), int(l))
        with self._lock:
            # a minute is only as healthy as its worst probe
            self._db.execute(
                "INSERT INTO minutes VALUES (?,?,?,?,?) ON CONFLICT(ts) DO UPDATE SET healthy=MIN(healthy,excluded.healthy),"
                " data_fresh=MIN(data_fresh,excluded.data_fresh), strategy_ok=MIN(strategy_ok,excluded.strategy_ok),"
                " live_locked=MIN(live_locked,excluded.live_locked)", row)
            self._db.execute("DELETE FROM minutes WHERE ts < ?", (t - KEEP_DAYS * DAY,))
            self._db.commit()
        return {"ts": t, "healthy": bool(row[1])}

    def summary(self, days: int = 30, *, outage_min: int = 5, max_outages: int = 10) -> dict:
        now = self.now_ms() // MIN * MIN - MIN  # last complete minute (the current one may not be written yet)
        start = now - days * DAY + MIN
        with self._lock:
            first = self._db.execute("SELECT MIN(ts) FROM minutes").fetchone()[0]
            rows = self._db.execute("SELECT ts, healthy FROM minutes WHERE ts >= ? AND ts <= ? ORDER BY ts", (start, now)).fetchall()
        if first is None or first > now:
            return {"windowDays": days, "since": None, "measuredMin": 0, "upPct": None, "healthyPct": None, "days": [], "outages": []}
        since = max(start, first)
        expected = (now - since) // MIN + 1
        up = len(rows)
        healthy = sum(r[1] for r in rows)
        # per BJ day
        per: dict[str, list[int]] = {}
        for ts, h in rows:
            d = per.setdefault(_bj_day(ts), [0, 0])
            d[0] += 1
            d[1] += h
        out_days = []
        day0 = (since + 8 * 3_600_000) // DAY * DAY - 8 * 3_600_000  # BJ midnight
        t = day0
        while t <= now:
            lo, hi = max(t, since), min(t + DAY - MIN, now)
            exp = (hi - lo) // MIN + 1 if hi >= lo else 0
            u, h = per.get(_bj_day(t), [0, 0])
            out_days.append({"day": _bj_day(t), "measuredMin": exp, "upPct": round(u / exp, 5) if exp else None,
                             "healthyPct": round(h / exp, 5) if exp else None})
            t += DAY
        # gaps (no row = down) and runs of unhealthy minutes (degraded), at least outage_min long
        events = []

        def close_run(start, n):
            if start is not None and n >= outage_min:
                events.append({"start": start, "minutes": n, "kind": "degraded"})

        prev, run_start, run_n = since - MIN, None, 0
        for ts, h in rows:
            gap = (ts - prev) // MIN - 1
            if gap > 0:
                close_run(run_start, run_n)
                run_start, run_n = None, 0
                if gap >= outage_min:
                    events.append({"start": prev + MIN, "minutes": gap, "kind": "down"})
            if h:
                close_run(run_start, run_n)
                run_start, run_n = None, 0
            else:
                run_start, run_n = (ts, 1) if run_start is None else (run_start, run_n + 1)
            prev = ts
        close_run(run_start, run_n)
        tail = (now - prev) // MIN  # minutes after the last row up to the last complete minute
        if tail >= outage_min:
            events.append({"start": prev + MIN, "minutes": tail, "kind": "down"})
        events.sort(key=lambda e: e["start"], reverse=True)
        return {"windowDays": days, "since": since, "measuredMin": expected, "upPct": round(up / expected, 5),
                "healthyPct": round(healthy / expected, 5), "days": out_days, "outages": events[:max_outages]}


_uptime: Optional[Uptime] = None
_ulock = threading.Lock()


def get_uptime() -> Uptime:
    global _uptime
    with _ulock:
        if _uptime is None:
            _uptime = Uptime()
        return _uptime


def reset_uptime(u: Optional[Uptime] = None) -> None:
    global _uptime
    with _ulock:
        _uptime = u


def current_checks() -> dict:
    """The coarse checks, from the same sources /api/v1/health uses."""
    from app.routes.health import _live_fields, _mainstream_fields, _strategy_status

    md = _mainstream_fields()
    st = _strategy_status()
    live = _live_fields()
    return {
        "data_fresh": bool(md.get("enabled")) and not md.get("stale"),
        "strategy_ok": not st.get("stalled"),
        "live_locked": live.get("liveEnabled") is False,
        "_md": md,
        "_st": st,
    }


def public_status(now_ms: Optional[int] = None) -> dict:
    """Login-free status payload: coarse fields only."""
    now = int(time.time() * 1000) if now_ms is None else now_ms
    c = current_checks()
    md, st = c["_md"], c["_st"]
    last_day = st.get("lastDay")
    try:
        summary = get_uptime().summary(30) if uptime_enabled() else None
    except Exception as exc:  # status must answer even if the record is broken
        log.warning("uptime summary: %s", type(exc).__name__)
        summary = None
    blocked = md.get("blocked") or {}
    return {
        "api": "up",
        "checkedAt": now,
        "healthy": bool(c["data_fresh"] and c["strategy_ok"] and c["live_locked"]),
        "liveTrading": "locked" if c["live_locked"] else "unlocked",
        "marketData": {"fresh": c["data_fresh"], "staleSeries": len(md.get("staleSeries") or []),
                       "exchangesBlocked": len(blocked), "exchangesTotal": len(md.get("exchanges") or []),
                       "lastRefreshMs": md.get("lastRefreshMs")},
        "strategy": {"active": bool(st.get("active")), "lastRebalanceDay": last_day, "lastRebalanceAt": st.get("lastRunAt"),
                     "hoursSinceClose": st.get("hoursSinceRebalance"), "overdueAfterH": st.get("stallHours"),
                     "overdue": bool(st.get("stalled"))},
        "uptime30d": summary,
    }


async def run_loop(interval_sec: int = 60) -> None:
    import asyncio

    await asyncio.sleep(30)  # after startup and the first market refresh
    u = get_uptime()
    while True:
        try:
            c = await asyncio.to_thread(current_checks)
            await asyncio.to_thread(u.record, c)
        except Exception:
            log.exception("uptime loop")
        await asyncio.sleep(interval_sec)
