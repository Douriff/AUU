"""SQLite store for mainstream klines + funding (one file under data_dir())."""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Iterable, Optional

from app.data_paths import data_dir

_SCHEMA = """
CREATE TABLE IF NOT EXISTS candles (
  exchange TEXT NOT NULL, symbol TEXT NOT NULL, tf TEXT NOT NULL, ts INTEGER NOT NULL,
  open REAL, high REAL, low REAL, close REAL, volume REAL,
  PRIMARY KEY (exchange, symbol, tf, ts)
);
CREATE TABLE IF NOT EXISTS funding (
  exchange TEXT NOT NULL, symbol TEXT NOT NULL, ts INTEGER NOT NULL, rate REAL NOT NULL,
  PRIMARY KEY (exchange, symbol, ts)
);
CREATE TABLE IF NOT EXISTS fetch_log (
  exchange TEXT NOT NULL, symbol TEXT NOT NULL, kind TEXT NOT NULL,
  last_attempt_ms INTEGER, last_ok_ms INTEGER, last_error TEXT, rows_added INTEGER,
  PRIMARY KEY (exchange, symbol, kind)
);
"""


def default_path() -> Path:
    return data_dir() / "mainstream.sqlite"


class MarketStore:
    def __init__(self, path: Optional[Path | str] = None):
        self.path = Path(path) if path else default_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False, timeout=30)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---- candles -------------------------------------------------------
    def upsert_candles(self, exchange: str, symbol: str, tf: str, rows: Iterable[list]) -> int:
        data = [
            (exchange, symbol, tf, int(r[0]), r[1], r[2], r[3], r[4], r[5])
            for r in rows
            if r and r[0] is not None and r[4] is not None
        ]
        if not data:
            return 0
        with self._lock:
            before = self._count("candles", exchange, symbol, tf)
            self._conn.executemany(
                "INSERT OR REPLACE INTO candles VALUES (?,?,?,?,?,?,?,?,?)", data
            )
            self._conn.commit()
            return self._count("candles", exchange, symbol, tf) - before

    def _count(self, table: str, exchange: str, symbol: str, tf: Optional[str] = None) -> int:
        if tf is None:
            q = f"SELECT COUNT(*) FROM {table} WHERE exchange=? AND symbol=?"
            return self._conn.execute(q, (exchange, symbol)).fetchone()[0]
        q = f"SELECT COUNT(*) FROM {table} WHERE exchange=? AND symbol=? AND tf=?"
        return self._conn.execute(q, (exchange, symbol, tf)).fetchone()[0]

    def candle_bounds(self, exchange: str, symbol: str, tf: str) -> tuple[Optional[int], Optional[int], int]:
        with self._lock:
            row = self._conn.execute(
                "SELECT MIN(ts), MAX(ts), COUNT(*) FROM candles WHERE exchange=? AND symbol=? AND tf=?",
                (exchange, symbol, tf),
            ).fetchone()
        return row[0], row[1], row[2]

    def candles(
        self,
        exchange: str,
        symbol: str,
        tf: str,
        *,
        since: Optional[int] = None,
        until: Optional[int] = None,
        limit: int = 500,
    ) -> list[dict]:
        q = "SELECT ts, open, high, low, close, volume FROM candles WHERE exchange=? AND symbol=? AND tf=?"
        args: list = [exchange, symbol, tf]
        if since is not None:
            q += " AND ts>=?"
            args.append(int(since))
        if until is not None:
            q += " AND ts<=?"
            args.append(int(until))
        q += " ORDER BY ts DESC LIMIT ?"
        args.append(int(limit))
        with self._lock:
            rows = self._conn.execute(q, args).fetchall()
        return [dict(r) for r in reversed(rows)]

    def range_stats(self, exchange: str, symbol: str, tf: str, lo: int, hi: int) -> tuple[Optional[int], Optional[int], int]:
        with self._lock:
            row = self._conn.execute(
                "SELECT MIN(ts), MAX(ts), COUNT(*) FROM candles WHERE exchange=? AND symbol=? AND tf=? AND ts>=? AND ts<=?",
                (exchange, symbol, tf, int(lo), int(hi)),
            ).fetchone()
        return row[0], row[1], row[2]

    def prune_candles(self, exchange: str, symbol: str, tf: str, older_than: int) -> int:
        """Drop candles with ts < older_than (rolling window for intraday timeframes)."""
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM candles WHERE exchange=? AND symbol=? AND tf=? AND ts<?",
                (exchange, symbol, tf, int(older_than)),
            )
            self._conn.commit()
            return cur.rowcount or 0

    def candle_ts(self, exchange: str, symbol: str, tf: str) -> list[int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT ts FROM candles WHERE exchange=? AND symbol=? AND tf=? ORDER BY ts",
                (exchange, symbol, tf),
            ).fetchall()
        return [r[0] for r in rows]

    def find_gaps(self, exchange: str, symbol: str, tf: str, step_ms: int) -> list[tuple[int, int]]:
        """Missing candle ranges ``(first_missing_ts, last_missing_ts)`` inside the stored span."""
        ts = self.candle_ts(exchange, symbol, tf)
        gaps = []
        for a, b in zip(ts, ts[1:]):
            if b - a > step_ms:
                gaps.append((a + step_ms, b - step_ms))
        return gaps

    # ---- funding -------------------------------------------------------
    def upsert_funding(self, exchange: str, symbol: str, rows: Iterable[tuple[int, float]]) -> int:
        data = [(exchange, symbol, int(ts), float(rate)) for ts, rate in rows if ts is not None and rate is not None]
        if not data:
            return 0
        with self._lock:
            before = self._count("funding", exchange, symbol)
            self._conn.executemany("INSERT OR REPLACE INTO funding VALUES (?,?,?,?)", data)
            self._conn.commit()
            return self._count("funding", exchange, symbol) - before

    def funding_bounds(self, exchange: str, symbol: str) -> tuple[Optional[int], Optional[int], int]:
        with self._lock:
            row = self._conn.execute(
                "SELECT MIN(ts), MAX(ts), COUNT(*) FROM funding WHERE exchange=? AND symbol=?",
                (exchange, symbol),
            ).fetchone()
        return row[0], row[1], row[2]

    def funding(self, exchange: str, symbol: str, *, since: Optional[int] = None, limit: int = 200) -> list[dict]:
        q = "SELECT ts, rate FROM funding WHERE exchange=? AND symbol=?"
        args: list = [exchange, symbol]
        if since is not None:
            q += " AND ts>=?"
            args.append(int(since))
        q += " ORDER BY ts DESC LIMIT ?"
        args.append(int(limit))
        with self._lock:
            rows = self._conn.execute(q, args).fetchall()
        return [dict(r) for r in reversed(rows)]

    # ---- fetch log -----------------------------------------------------
    def log_fetch(
        self, exchange: str, symbol: str, kind: str, *, attempt_ms: int, ok: bool, error: str | None, rows: int
    ) -> None:
        with self._lock:
            prev = self._conn.execute(
                "SELECT last_ok_ms FROM fetch_log WHERE exchange=? AND symbol=? AND kind=?",
                (exchange, symbol, kind),
            ).fetchone()
            last_ok = attempt_ms if ok else (prev[0] if prev else None)
            self._conn.execute(
                "INSERT OR REPLACE INTO fetch_log VALUES (?,?,?,?,?,?,?)",
                (exchange, symbol, kind, attempt_ms, last_ok, None if ok else (error or "error")[:300], rows),
            )
            self._conn.commit()

    def fetch_log(self, exchange: str) -> dict[tuple[str, str], dict]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM fetch_log WHERE exchange=?", (exchange,)).fetchall()
        return {(r["symbol"], r["kind"]): dict(r) for r in rows}
