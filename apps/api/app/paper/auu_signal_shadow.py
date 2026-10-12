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
