"""Shadow log for ``auu_signal_v0`` — record only, never drives live/paper rebalance.

Isolated SQLite ``auu_signal_shadow.sqlite`` under data_dir(). Does not open or write
mainstream_strategy / H2 / S3 / user paper ledgers. Hash of the trading ledger is untouched.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

from app.data_paths import data_dir
from app.strategies.auu_signal_v0 import VERSION, AuuSignalV0, align_funding_to_bars

log = logging.getLogger("auu.signal")

LEDGER_NAME = "auu_signal_shadow.sqlite"
LABEL = "AUU signal shadow · experimental · record-only · not evidence until gate passes"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS bars (
  version TEXT NOT NULL,
  symbol TEXT NOT NULL,
  tf TEXT NOT NULL,
  asof_ts INTEGER NOT NULL,
  side TEXT NOT NULL,
  score REAL NOT NULL,
  action TEXT NOT NULL,
  reason_tags TEXT NOT NULL,
  factors TEXT NOT NULL,
  recorded_at INTEGER NOT NULL,
  PRIMARY KEY (version, symbol, tf, asof_ts)
);
CREATE INDEX IF NOT EXISTS bars_sym_tf ON bars(symbol, tf, asof_ts);
-- forward-shadow candidates (record-only, pre-registered; see docs/signal-candidates.md)
CREATE TABLE IF NOT EXISTS cand_bars (
  version TEXT NOT NULL, params_hash TEXT NOT NULL, symbol TEXT NOT NULL, tf TEXT NOT NULL, asof_ts INTEGER NOT NULL,
  side TEXT NOT NULL, score REAL NOT NULL, action TEXT NOT NULL, factors TEXT NOT NULL, recorded_at INTEGER NOT NULL,
  PRIMARY KEY (version, symbol, tf, asof_ts)
);
CREATE TABLE IF NOT EXISTS cand_trades (
  version TEXT NOT NULL, symbol TEXT NOT NULL, side TEXT NOT NULL, entry_ts INTEGER NOT NULL, entry_px REAL NOT NULL,
  exit_ts INTEGER, exit_px REAL, gross REAL, funding REAL, net REAL, recorded_at INTEGER NOT NULL,
  PRIMARY KEY (version, symbol, entry_ts)
);
CREATE TABLE IF NOT EXISTS cand_nav (
  version TEXT NOT NULL, ts INTEGER NOT NULL, ret REAL NOT NULL, nav REAL NOT NULL, n_long INTEGER NOT NULL,
  n_short INTEGER NOT NULL, n_sym INTEGER NOT NULL, recorded_at INTEGER NOT NULL,
  PRIMARY KEY (version, ts)
);
"""

TF_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}


def shadow_enabled() -> bool:
    from app.role import is_standby

    if is_standby():
        return False
    return os.getenv("AUU_SIGNAL_SHADOW", "on").strip().lower() not in {"0", "false", "off", "no"}


class SignalShadowStore:
    def __init__(self, path: Optional[Path | str] = None, *, now_ms: Callable[[], int] = lambda: int(time.time() * 1000)):
        self.path = Path(path) if path else data_dir() / LEDGER_NAME
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.now_ms = now_ms
        self._db = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None, timeout=30)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(_SCHEMA)
        self._db.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('version', ?)", (VERSION,))
        self._db.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('label', ?)", (LABEL,))
        self._lock = threading.RLock()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def upsert_rows(self, symbol: str, tf: str, rows: list[dict], *, version: str = VERSION) -> int:
        if not rows:
            return 0
        now = self.now_ms()
        data = [
            (
                version,
                symbol,
                tf,
                int(r["asof_ts"]),
                r["side"],
                float(r["score"]),
                r["action"],
                json.dumps(r.get("reason_tags") or [], separators=(",", ":")),
                json.dumps(r.get("factors") or {}, separators=(",", ":")),
                now,
            )
            for r in rows
        ]
        with self._lock:
            self._db.executemany(
                "INSERT OR REPLACE INTO bars(version,symbol,tf,asof_ts,side,score,action,reason_tags,factors,recorded_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                data,
            )
            return len(data)

    def series(self, symbol: str, tf: str = "1d", *, limit: int = 400, version: str = VERSION) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM bars WHERE version=? AND symbol=? AND tf=? ORDER BY asof_ts DESC LIMIT ?",
                (version, symbol, tf, int(limit)),
            ).fetchall()
        out = []
        for r in reversed(rows):
            out.append(
                {
                    "asof_ts": r["asof_ts"],
                    "side": r["side"],
                    "score": r["score"],
                    "action": r["action"],
                    "reason_tags": json.loads(r["reason_tags"] or "[]"),
                    "factors": json.loads(r["factors"] or "{}"),
                    "recorded_at": r["recorded_at"],
                }
            )
        return out

    def latest(self, symbol: str, tf: str = "1d", *, version: str = VERSION) -> Optional[dict]:
        rows = self.series(symbol, tf, limit=1, version=version)
        return rows[-1] if rows else None

    def count(self, symbol: Optional[str] = None, *, version: str = VERSION) -> int:
        with self._lock:
            if symbol:
                return self._db.execute(
                    "SELECT COUNT(*) FROM bars WHERE version=? AND symbol=?", (version, symbol)
                ).fetchone()[0]
            return self._db.execute("SELECT COUNT(*) FROM bars WHERE version=?", (version,)).fetchone()[0]


    # ---- forward candidates (record-only) ----
    def upsert_cand_bars(self, version: str, phash: str, symbol: str, tf: str, rows: list[dict]) -> int:
        now = self.now_ms()
        data = [(version, phash, symbol, tf, int(r["asof_ts"]), r["side"], float(r["score"]), r["action"],
                 json.dumps(r.get("factors") or {}, separators=(",", ":")), now) for r in rows]
        with self._lock:
            self._db.executemany("INSERT OR REPLACE INTO cand_bars VALUES (?,?,?,?,?,?,?,?,?,?)", data)
        return len(data)

    def replace_cand_trades(self, version: str, symbol: str, trades: list[dict]) -> int:
        now = self.now_ms()
        with self._lock:
            self._db.execute("BEGIN")
            self._db.execute("DELETE FROM cand_trades WHERE version=? AND symbol=?", (version, symbol))
            self._db.executemany(
                "INSERT INTO cand_trades VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                [(version, symbol, t["side"], t["entry_ts"], t["entry_px"], t["exit_ts"], t["exit_px"],
                  t["gross"], t["funding"], t["net"], now) for t in trades])
            self._db.execute("COMMIT")
        return len(trades)

    def replace_cand_nav(self, version: str, rows: list[tuple[int, float, float, int, int, int]]) -> int:
        now = self.now_ms()
        with self._lock:
            self._db.execute("BEGIN")
            self._db.execute("DELETE FROM cand_nav WHERE version=?", (version,))
            self._db.executemany("INSERT INTO cand_nav VALUES (?,?,?,?,?,?,?,?)", [(version, *r, now) for r in rows])
            self._db.execute("COMMIT")
        return len(rows)


_store: Optional[SignalShadowStore] = None
_store_lock = threading.Lock()


def get_store() -> SignalShadowStore:
    global _store
    with _store_lock:
        if _store is None:
            _store = SignalShadowStore()
        return _store


def reset_store_for_tests() -> None:
    global _store
    with _store_lock:
        if _store is not None:
            try:
                _store.close()
            except Exception:
                pass
            _store = None


def build_series_from_candles(
    candles: list[dict],
    *,
    funding_rows: Optional[list[tuple[int, float]]] = None,
    tf: str = "1d",
    drop_forming: bool = True,
    now_ms: Optional[int] = None,
    engine: Optional[AuuSignalV0] = None,
) -> list[dict]:
    """Compute overlay from store candle dicts ``{ts,open,high,low,close,volume}``.

    When ``drop_forming`` is True, the last bar is dropped if its close is still in the future
    (strict asof: only closed bars).
    """
    if not candles:
        return []
    bar_ms = TF_MS.get(tf, 86_400_000)
    rows = list(candles)
    now = int(time.time() * 1000) if now_ms is None else int(now_ms)
    if drop_forming and rows:
        last_close = int(rows[-1]["ts"]) + bar_ms
        if last_close > now:
            rows = rows[:-1]
    if not rows:
        return []
    closes = [float(c["close"]) if c.get("close") is not None else None for c in rows]
    volumes = [float(c["volume"]) if c.get("volume") is not None else None for c in rows]
    ts = [int(c["ts"]) for c in rows]
    funding = align_funding_to_bars(ts, funding_rows or [], bar_ms=bar_ms) if funding_rows is not None else None
    eng = engine or AuuSignalV0()
    return eng.compute(closes=closes, volumes=volumes, ts=ts, funding=funding)


def compute_and_record(
    symbol: str,
    candles: list[dict],
    *,
    tf: str = "1d",
    funding_rows: Optional[list[tuple[int, float]]] = None,
    store: Optional[SignalShadowStore] = None,
    now_ms: Optional[int] = None,
) -> dict[str, Any]:
    """Compute series, upsert into shadow store, return API payload (experimental)."""
    series = build_series_from_candles(candles, funding_rows=funding_rows, tf=tf, now_ms=now_ms)
    st = store or get_store()
    written = st.upsert_rows(symbol, tf, series)
    latest = series[-1] if series else None
    return {
        "version": VERSION,
        "experimental": True,
        "label": LABEL,
        "symbol": symbol,
        "tf": tf,
        "series": series,
        "latest": latest,
        "shadowWritten": written,
        "shadowCount": st.count(symbol),
        "params": AuuSignalV0().describe(),
    }


async def run_loop(interval_sec: int = 600) -> None:
    """Periodic shadow refresh for strategy-universe coins on 1d (record-only)."""
    import asyncio

    while True:
        try:
            if shadow_enabled():
                _tick_universe()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("auu_signal shadow tick failed")
        await asyncio.sleep(interval_sec)


def _tick_universe() -> None:
    from app.marketdata.mainstream import get_service

    svc = get_service()
    ex = svc.exchange_for_read()
    if not ex:
        return
    store = get_store()
    for base in svc.cfg.strategy_symbols:
        try:
            candles = svc.store.candles(ex, base, "1d", limit=500)
            fr = svc.store.funding(ex, base, limit=500)
            funding_rows = [(int(r["ts"]), float(r["rate"])) for r in fr]
            compute_and_record(base, candles, tf="1d", funding_rows=funding_rows, store=store, now_ms=svc.now_ms())
        except Exception:
            log.exception("auu_signal shadow %s failed", base)
    try:
        record_candidates(svc, ex, store)
    except Exception:
        log.exception("auu_signal candidates shadow failed")


def record_candidates(svc: Any, ex: str, store: SignalShadowStore) -> None:
    """Forward shadow for pre-registered candidates + v0 baseline: bars, hypothetical trades, NAV.
    Record-only; trades/NAV only from REG_TS (bars after registration)."""
    from app.strategies import auu_signal_candidates as C

    bar_ms = TF_MS["1d"]
    now = svc.now_ms()
    per_version: dict[str, dict[str, tuple[list[dict], dict[int, float], list[tuple[int, float]]]]] = {}
    for base in svc.cfg.strategy_symbols:
        candles = svc.store.candles(ex, base, "1d", limit=500)
        fr = [(int(r["ts"]), float(r["rate"])) for r in svc.store.funding(ex, base, limit=500)]
        opens = {int(c["ts"]): float(c["open"]) for c in candles if c.get("open") is not None}
        closed = [c for c in candles if int(c["ts"]) + bar_ms <= now]
        if not closed:
            continue
        closes = [float(c["close"]) if c.get("close") is not None else None for c in closed]
        vols = [float(c["volume"]) if c.get("volume") is not None else None for c in closed]
        ts = [int(c["ts"]) for c in closed]
        fund = align_funding_to_bars(ts, fr, bar_ms=bar_ms)
        v0rows = AuuSignalV0().compute(closes=closes, volumes=vols, ts=ts, funding=fund)
        per_version.setdefault(VERSION, {})[base] = (v0rows, opens, fr)
        for cid in C.CANDIDATES:
            rows = C.compute_candidate(cid, closes=closes, volumes=vols, ts=ts, funding=fund)
            store.upsert_cand_bars(cid, C.PARAMS_HASH[cid], base, "1d", [r for r in rows if r["asof_ts"] >= C.REG_TS - 30 * bar_ms])
            per_version.setdefault(cid, {})[base] = (rows, opens, fr)
    for version, bysym in per_version.items():
        for base, (rows, opens, fr) in bysym.items():
            store.replace_cand_trades(version, base, C.hypothetical_trades(rows, opens, bar_ms=bar_ms, funding_rows=fr, since_ts=C.REG_TS))
        store.replace_cand_nav(version, forward_nav(bysym, bar_ms=bar_ms, since_ts=C.REG_TS))


def forward_nav(bysym: dict, *, bar_ms: int, since_ts: int, cost: float = 0.0007) -> list[tuple[int, float, float, int, int, int]]:
    """Equal-weight daily NAV: side at bar t held open[t+1]->open[t+2]; cost on change; funding on longs/shorts.
    Only bars t >= since_ts; prior position before since_ts counts as flat (forward-only)."""
    days: dict[int, list[tuple[float, int]]] = {}
    for rows, opens, fr in bysym.values():
        prev = 0
        for r in rows:
            t = int(r["asof_ts"])
            if t < since_ts:
                continue
            o1, o2 = opens.get(t + bar_ms), opens.get(t + 2 * bar_ms)
            if o1 is None or o2 is None:
                break
            p = {"long": 1, "short": -1}.get(r["side"], 0)
            f = sum(x for ft, x in fr if t + bar_ms < ft <= t + 2 * bar_ms)
            ret = p * (o2 / o1 - 1) - abs(p - prev) * cost - p * f
            days.setdefault(t + bar_ms, []).append((ret, p))
            prev = p
    out = []; nav = 1.0
    for d in sorted(days):
        xs = days[d]; r = sum(x for x, _ in xs) / len(xs); nav *= 1 + r
        out.append((d, r, nav, sum(1 for _, p in xs if p > 0), sum(1 for _, p in xs if p < 0), len(xs)))
    return out
