"""Shadow log for ``auu_signal_v0`` — record only, never drives live/paper rebalance.

Isolated SQLite ``auu_signal_shadow.sqlite`` under data_dir(). Does not open or write
mainstream_strategy / H2 / S3 / user paper ledgers. Hash of the trading ledger is untouched.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone
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
        # write accounting (rows actually written / skipped as unchanged / commits); read by the tick log
        self.stats = {"rows_written": 0, "rows_skipped": 0, "commits": 0}

    def _begin(self) -> None:
        # isolation_level=None => autocommit; every batch write is ONE explicit transaction (one WAL commit/fsync)
        # instead of one implicit commit per row. IMMEDIATE: take the write lock up front (other writers wait on
        # the 30s busy timeout rather than failing on a stale read snapshot).
        self._db.execute("BEGIN IMMEDIATE")

    def _commit(self) -> None:
        self._db.execute("COMMIT")
        self.stats["commits"] += 1

    def _rollback(self) -> None:
        try:
            self._db.execute("ROLLBACK")
        except sqlite3.Error:
            pass

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def upsert_rows(
        self, symbol: str, tf: str, rows: list[dict], *, version: str = VERSION, skip_unchanged: bool = False
    ) -> int:
        """Upsert signal rows in ONE transaction.

        ``skip_unchanged=False`` (default, what the HTTP routes use): every row is written, ``recorded_at`` = now,
        exactly as before (only the commit granularity changed). ``skip_unchanged=True`` (background tick): a row
        whose side/score/action/reason_tags/factors equal the stored row is not rewritten, so its ``recorded_at``
        keeps the time it was first recorded. Returns the number of rows written.
        """
        if not rows:
            return 0
        now = self.now_ms()
        by_ts: dict[int, tuple] = {}
        for r in rows:  # last duplicate wins, same as sequential INSERT OR REPLACE
            by_ts[int(r["asof_ts"])] = (
                r["side"],
                float(r["score"]),
                r["action"],
                json.dumps(r.get("reason_tags") or [], separators=(",", ":")),
                json.dumps(r.get("factors") or {}, separators=(",", ":")),
            )
        with self._lock:
            todo = list(by_ts.items())
            if skip_unchanged:
                lo, hi = min(by_ts), max(by_ts)
                have = {
                    r[0]: tuple(r[1:])
                    for r in self._db.execute(
                        "SELECT asof_ts, side, score, action, reason_tags, factors FROM bars "
                        "WHERE version=? AND symbol=? AND tf=? AND asof_ts BETWEEN ? AND ?",
                        (version, symbol, tf, lo, hi),
                    )
                }
                todo = [(t, v) for t, v in todo if have.get(t) != v]
            if todo:  # nothing to write => no transaction, no commit, no fsync
                self._begin()
                try:
                    self._db.executemany(
                        "INSERT OR REPLACE INTO bars(version,symbol,tf,asof_ts,side,score,action,reason_tags,factors,recorded_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?)",
                        [(version, symbol, tf, t, *v, now) for t, v in todo],
                    )
                    self._commit()
                except BaseException:
                    self._rollback()
                    raise
            if skip_unchanged:
                self.stats["rows_written"] += len(todo)
                self.stats["rows_skipped"] += len(by_ts) - len(todo)
                return len(todo)
            self.stats["rows_written"] += len(rows)
            return len(rows)

    def touch_latest(self, symbols: list[str], tf: str = "1d", *, version: str = VERSION) -> None:
        """Liveness heartbeat for the background tick: stamp ``recorded_at`` = now on the newest stored bar of each
        symbol (and ``meta.last_tick_ms``), so ``max(recorded_at)`` keeps meaning "the shadow loop is alive" even
        though unchanged rows are no longer rewritten every tick. One transaction, a handful of rows."""
        now = self.now_ms()
        with self._lock:
            self._begin()
            try:
                for sym in symbols:
                    self._db.execute(
                        "UPDATE bars SET recorded_at=? WHERE version=? AND symbol=? AND tf=? AND asof_ts="
                        "(SELECT MAX(asof_ts) FROM bars WHERE version=? AND symbol=? AND tf=?)",
                        (now, version, sym, tf, version, sym, tf),
                    )
                self._db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('last_tick_ms', ?)", (str(now),))
                self._commit()
            except BaseException:
                self._rollback()
                raise

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
    def upsert_cand_bars(
        self, version: str, phash: str, symbol: str, tf: str, rows: list[dict], *, skip_unchanged: bool = False
    ) -> int:
        """One transaction per call (was: one autocommit per row). ``skip_unchanged`` as in :meth:`upsert_rows`."""
        if not rows:
            return 0
        now = self.now_ms()
        by_ts: dict[int, tuple] = {}
        for r in rows:
            by_ts[int(r["asof_ts"])] = (
                phash, r["side"], float(r["score"]), r["action"], json.dumps(r.get("factors") or {}, separators=(",", ":"))
            )
        with self._lock:
            todo = list(by_ts.items())
            if skip_unchanged:
                lo, hi = min(by_ts), max(by_ts)
                have = {
                    r[0]: tuple(r[1:])
                    for r in self._db.execute(
                        "SELECT asof_ts, params_hash, side, score, action, factors FROM cand_bars "
                        "WHERE version=? AND symbol=? AND tf=? AND asof_ts BETWEEN ? AND ?",
                        (version, symbol, tf, lo, hi),
                    )
                }
                todo = [(t, v) for t, v in todo if have.get(t) != v]
            if todo:
                self._begin()
                try:
                    self._db.executemany(
                        "INSERT OR REPLACE INTO cand_bars VALUES (?,?,?,?,?,?,?,?,?,?)",
                        [(version, v[0], symbol, tf, t, v[1], v[2], v[3], v[4], now) for t, v in todo],
                    )
                    self._commit()
                except BaseException:
                    self._rollback()
                    raise
            if skip_unchanged:
                self.stats["rows_written"] += len(todo)
                self.stats["rows_skipped"] += len(by_ts) - len(todo)
                return len(todo)
            self.stats["rows_written"] += len(rows)
            return len(rows)

    def replace_cand_trades(self, version: str, symbol: str, trades: list[dict], *, skip_unchanged: bool = False) -> int:
        now = self.now_ms()
        new = [(t["side"], t["entry_ts"], t["entry_px"], t["exit_ts"], t["exit_px"], t["gross"], t["funding"], t["net"])
               for t in trades]
        with self._lock:
            if skip_unchanged:
                have = [tuple(r) for r in self._db.execute(
                    "SELECT side, entry_ts, entry_px, exit_ts, exit_px, gross, funding, net FROM cand_trades "
                    "WHERE version=? AND symbol=? ORDER BY entry_ts", (version, symbol))]
                if have == sorted(new, key=lambda x: x[1]):
                    self.stats["rows_skipped"] += len(new)
                    return len(trades)
            self._begin()
            try:
                self._db.execute("DELETE FROM cand_trades WHERE version=? AND symbol=?", (version, symbol))
                self._db.executemany(
                    "INSERT INTO cand_trades VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    [(version, symbol, t["side"], t["entry_ts"], t["entry_px"], t["exit_ts"], t["exit_px"],
                      t["gross"], t["funding"], t["net"], now) for t in trades])
                self._commit()
            except BaseException:
                self._rollback()
                raise
            self.stats["rows_written"] += len(trades)
        return len(trades)

    def replace_cand_nav(self, version: str, rows: list[tuple[int, float, float, int, int, int]], *, skip_unchanged: bool = False) -> int:
        now = self.now_ms()
        with self._lock:
            if skip_unchanged:
                have = [tuple(r) for r in self._db.execute(
                    "SELECT ts, ret, nav, n_long, n_short, n_sym FROM cand_nav WHERE version=? ORDER BY ts", (version,))]
                if have == sorted([tuple(r) for r in rows], key=lambda x: x[0]):
                    self.stats["rows_skipped"] += len(rows)
                    return len(rows)
            self._begin()
            try:
                self._db.execute("DELETE FROM cand_nav WHERE version=?", (version,))
                self._db.executemany("INSERT INTO cand_nav VALUES (?,?,?,?,?,?,?,?)", [(version, *r, now) for r in rows])
                self._commit()
            except BaseException:
                self._rollback()
                raise
            self.stats["rows_written"] += len(rows)
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


# ---------------------------------------------------------------------------------------------------------------------
# Background tick
#
# Cost model (production-sized data: 19 coins x 500 daily bars x v0 + 4 candidates): a full recompute is 8-30 s of pure
# Python plus ~13k row writes. The daily-bar signals only change when a new daily bar closes (00:00 UTC), when the
# funding history gets a new settlement, or when stored history is repaired. So the tick is split in two levels:
#
#   probe  (every AUU_SIGNAL_SHADOW_PROBE_SEC, default 120 s): 3 indexed queries per coin on the market store; if the
#          newest closed 1d bars / last candle open / last funding point of any coin differ from what the last
#          successful tick saw, run a full check now. A new daily bar is therefore processed <= ~2 min after it
#          lands in the market store (which itself refreshes every AUU_MAINSTREAM_REFRESH_SEC, default 300 s).
#   full check (every AUU_SIGNAL_SHADOW_INTERVAL_SEC, default 600 s, 60..1800): reads the 500-bar inputs of every coin
#          and fingerprints them (exact inputs of the computation: closed bars' ts/close/volume, opens, funding, plus
#          which bars are closed at ``now``). Unchanged fingerprint => nothing is recomputed or written. Changed =>
#          recompute (same functions as before) and write only rows that differ from the stored row.
#
# Everything runs in a worker thread (asyncio.to_thread) with its own SQLite connection to the shadow DB, opened and
# closed inside that thread; the market store is the shared, lock-protected MarketStore (read only here).
# ---------------------------------------------------------------------------------------------------------------------
DAY_MS = TF_MS["1d"]


def _env_int(name: str, default: int, lo: int, hi: int) -> int:
    try:
        v = int(float(os.getenv(name, "").strip() or default))
    except ValueError:
        v = default
    return max(lo, min(hi, v))


def tick_intervals(interval_sec: Optional[int] = None, probe_sec: Optional[int] = None) -> tuple[int, int]:
    """(full-check interval, probe interval) in seconds. Interval 60..1800 (default 600), probe 30..600 (default 120),
    probe never longer than the interval."""
    interval = _env_int("AUU_SIGNAL_SHADOW_INTERVAL_SEC", 600, 60, 1800) if interval_sec is None else max(1, int(interval_sec))
    probe = _env_int("AUU_SIGNAL_SHADOW_PROBE_SEC", 120, 30, 600) if probe_sec is None else max(1, int(probe_sec))
    return interval, min(probe, interval)


def _force_sec() -> int:
    """Every N seconds ignore fingerprints and recompute everything (row-level diff still skips unchanged rows):
    self-heal if the shadow DB was edited/restored behind our back. Default 6 h."""
    return _env_int("AUU_SIGNAL_SHADOW_FORCE_SEC", 21600, 600, 7 * 86400)


def _iso(ms: Optional[float] = None) -> str:
    t = time.time() if ms is None else ms / 1000.0
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _ensure_log_visible() -> None:
    """The process has no root logging config (plain ``uvicorn app.main:app``), so INFO from our own logger would be
    dropped. Attach a stderr handler to the ``auu.signal`` logger only, once, and only if nothing else handles it."""
    if log.handlers or logging.getLogger().handlers:
        return
    h = logging.StreamHandler()
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    log.addHandler(h)
    log.setLevel(logging.INFO)


class TickState:
    """What the last successful tick saw. Only touched from the (sequential) tick worker; the lock is belt and braces."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.probe_sig: Optional[tuple] = None
        self.v0_fp: dict[str, str] = {}
        self.cand_fp: Optional[str] = None
        self.last_force_mono: Optional[float] = None
        self.db_ino: Optional[int] = None


def _digest(*parts: Any) -> str:
    h = hashlib.blake2b(digest_size=16)
    for p in parts:
        h.update(repr(p).encode())
        h.update(b"|")
    return h.hexdigest()


def _fp_v0(candles: list[dict], funding_rows: list[tuple[int, float]], now: int) -> str:
    """Fingerprint of everything build_series_from_candles(tf='1d') reads."""
    rows = candles
    if rows and int(rows[-1]["ts"]) + DAY_MS > now:  # forming bar dropped, as build_series_from_candles does
        rows = rows[:-1]
    return _digest([(int(c["ts"]), c.get("close"), c.get("volume")) for c in rows], funding_rows)


def _fp_cand(candles: list[dict], fr: list[tuple[int, float]], now: int) -> str:
    """Fingerprint of everything record_candidates reads for one coin: closed bars (ts/close/volume), every bar's open
    (next-bar-open fills use the forming bar's open), funding rows."""
    closed = [(int(c["ts"]), c.get("close"), c.get("volume")) for c in candles if int(c["ts"]) + DAY_MS <= now]
    opens = [(int(c["ts"]), c["open"]) for c in candles if c.get("open") is not None]
    return _digest(closed, opens, fr)


def _load_inputs(svc: Any, ex: str, base: str) -> tuple[list[dict], list[tuple[int, float]]]:
    candles = svc.store.candles(ex, base, "1d", limit=500)
    funding_rows = [(int(r["ts"]), float(r["rate"])) for r in svc.store.funding(ex, base, limit=500)]
    return candles, funding_rows


def _probe_signature(svc: Any, ex: str, now: int) -> tuple:
    sig = []
    for base in svc.cfg.strategy_symbols:
        closed = svc.store.candles(ex, base, "1d", until=now - DAY_MS, limit=2)
        last = svc.store.candles(ex, base, "1d", limit=1)
        fund = svc.store.funding(ex, base, limit=1)
        sig.append((
            base,
            tuple((c["ts"], c.get("open"), c.get("close"), c.get("volume")) for c in closed),
            tuple((c["ts"], c.get("open")) for c in last),
            tuple((r["ts"], r["rate"]) for r in fund),
        ))
    return tuple(sig)


async def run_loop(interval_sec: Optional[int] = None, probe_sec: Optional[int] = None) -> None:
    """Periodic shadow refresh for strategy-universe coins on 1d (record-only).

    The synchronous work (SQLite reads, 8-30 s of CPU, writes) runs in a worker thread so the event loop (uvicorn,
    /api/v1/health) is never blocked by it."""
    import asyncio

    _ensure_log_visible()
    interval, probe = tick_intervals(interval_sec, probe_sec)
    state = TickState()
    last_full: Optional[float] = None
    while True:
        try:
            if shadow_enabled():
                mono = time.monotonic()
                full = last_full is None or mono - last_full >= interval
                if full:
                    last_full = mono
                await asyncio.to_thread(_tick_universe, state=state, full=full)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("auu_signal shadow tick failed")
        await asyncio.sleep(min(probe, interval))


def _tick_universe(
    *,
    state: Optional[TickState] = None,
    full: bool = True,
    force: Optional[bool] = None,
    store: Optional[SignalShadowStore] = None,
) -> dict[str, Any]:
    """One tick. SYNCHRONOUS and meant to run in a worker thread (``asyncio.to_thread``).

    ``state=None`` (or ``force=True``): recompute everything (still skipping rows identical to the stored ones).
    With a ``state``: ``full=False`` only probes (cheap) and returns without touching the shadow DB when nothing
    changed; ``full=True`` fingerprints inputs and recomputes only what changed.
    ``store``: tests may pass one; otherwise a private connection is opened here (in this thread) and closed on exit.
    """
    from app.marketdata.mainstream import get_service

    svc = get_service()
    ex = svc.exchange_for_read()
    if not ex:
        return {"skipped": "no_exchange", "full": False}
    if force is None:
        force = state is None
    symbols = list(svc.cfg.strategy_symbols)
    t0 = time.perf_counter()

    sig = _probe_signature(svc, ex, svc.now_ms()) if state is not None else None
    probe_changed = state is not None and sig != state.probe_sig
    if state is not None and not force:
        if not full and not probe_changed:
            log.debug("auu_signal shadow probe unchanged ts=%s", _iso())
            return {"skipped": "probe_unchanged", "full": False}
        if state.last_force_mono is None or time.monotonic() - state.last_force_mono >= _force_sec():
            force = True
    started = _iso()
    reason = "forced" if force else ("probe_changed" if probe_changed and not full else "scheduled")
    log.info("auu_signal shadow tick start ts=%s reason=%s coins=%d", started, reason, len(symbols))

    own = store is None
    st = store
    stats: dict[str, Any] = {"full": True, "force": force, "coins": len(symbols), "recomputed": 0, "unchanged": 0,
                             "cand_recomputed": False, "failed": 0}
    ok = True
    try:
        if st is None:
            st = SignalShadowStore()
        base_stats = dict(st.stats)
        if state is not None:
            try:
                ino = os.stat(st.path).st_ino
            except OSError:
                ino = None
            if state.db_ino is not None and ino != state.db_ino:
                force = True  # shadow DB file was replaced under us: recompute everything
            state.db_ino = ino
        inputs: dict[str, tuple[list[dict], list[tuple[int, float]]]] = {}
        new_v0_fp: dict[str, str] = {}
        for base in symbols:
            try:
                candles, funding_rows = _load_inputs(svc, ex, base)
                inputs[base] = (candles, funding_rows)
                now = svc.now_ms()
                fp = _fp_v0(candles, funding_rows, now)
                if state is not None and not force and state.v0_fp.get(base) == fp:
                    stats["unchanged"] += 1
                    continue
                series = build_series_from_candles(candles, funding_rows=funding_rows, tf="1d", now_ms=now)
                st.upsert_rows(base, "1d", series, skip_unchanged=True)
                new_v0_fp[base] = fp
                stats["recomputed"] += 1
            except Exception:
                ok = False
                stats["failed"] += 1
                log.exception("auu_signal shadow %s failed", base)
        try:
            stats["cand_recomputed"] = record_candidates(
                svc, ex, st, state=state, force=force, inputs=inputs)
        except Exception:
            ok = False
            log.exception("auu_signal candidates shadow failed")
        try:
            st.touch_latest(symbols)  # liveness heartbeat, see SignalShadowStore.touch_latest
        except Exception:
            ok = False
            log.exception("auu_signal shadow heartbeat failed")
        if state is not None:
            with state.lock:
                state.v0_fp.update(new_v0_fp)
                if force:
                    state.last_force_mono = time.monotonic()
                if ok:
                    state.probe_sig = sig
        stats["rows_written"] = st.stats["rows_written"] - base_stats["rows_written"]
        stats["rows_skipped"] = st.stats["rows_skipped"] - base_stats["rows_skipped"]
        stats["commits"] = st.stats["commits"] - base_stats["commits"]
    finally:
        if own and st is not None:
            try:
                st.close()
            except Exception:
                pass
    took_ms = (time.perf_counter() - t0) * 1000
    stats["took_ms"] = round(took_ms, 1)
    log.info(
        "auu_signal shadow tick done start=%s end=%s took_ms=%.0f recomputed=%d unchanged=%d candidates_recomputed=%s "
        "rows_written=%d rows_skipped=%d commits=%d failed=%d",
        started, _iso(), took_ms, stats["recomputed"], stats["unchanged"], stats["cand_recomputed"],
        stats["rows_written"], stats["rows_skipped"], stats["commits"], stats["failed"],
    )
    return stats


def record_candidates(
    svc: Any,
    ex: str,
    store: SignalShadowStore,
    *,
    state: Optional[TickState] = None,
    force: bool = True,
    inputs: Optional[dict[str, tuple[list[dict], list[tuple[int, float]]]]] = None,
) -> bool:
    """Forward shadow for pre-registered candidates + v0 baseline: bars, hypothetical trades, NAV.
    Record-only; trades/NAV only from REG_TS (bars after registration).

    Returns True when it recomputed, False when the fingerprint of all inputs matched the last tick (nothing to do).
    Written rows are diffed against the stored ones (unchanged rows are not rewritten)."""
    from app.strategies import auu_signal_candidates as C

    bar_ms = TF_MS["1d"]
    now = svc.now_ms()
    loaded: dict[str, tuple[list[dict], list[tuple[int, float]]]] = {}
    for base in svc.cfg.strategy_symbols:
        loaded[base] = (inputs or {}).get(base) or _load_inputs(svc, ex, base)
    if state is not None and not force:
        fp = _digest([(b, _fp_cand(*loaded[b], now)) for b in svc.cfg.strategy_symbols])
        if state.cand_fp == fp:
            return False
    else:
        fp = _digest([(b, _fp_cand(*loaded[b], now)) for b in svc.cfg.strategy_symbols]) if state is not None else None
    per_version: dict[str, dict[str, tuple[list[dict], dict[int, float], list[tuple[int, float]]]]] = {}
    for base in svc.cfg.strategy_symbols:
        candles, fr = loaded[base]
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
            store.upsert_cand_bars(cid, C.PARAMS_HASH[cid], base, "1d",
                                   [r for r in rows if r["asof_ts"] >= C.REG_TS - 30 * bar_ms], skip_unchanged=True)
            per_version.setdefault(cid, {})[base] = (rows, opens, fr)
    for version, bysym in per_version.items():
        for base, (rows, opens, fr) in bysym.items():
            store.replace_cand_trades(
                version, base, C.hypothetical_trades(rows, opens, bar_ms=bar_ms, funding_rows=fr, since_ts=C.REG_TS),
                skip_unchanged=True)
        store.replace_cand_nav(version, forward_nav(bysym, bar_ms=bar_ms, since_ts=C.REG_TS), skip_unchanged=True)
    if state is not None:
        with state.lock:
            state.cand_fp = fp
    return True


def forward_nav(bysym: dict, *, bar_ms: int, since_ts: int, cost: float = 0.0007) -> list[tuple[int, float, float, int, int, int]]:
    """Equal-weight daily NAV: side at bar t held open[t+1]->open[t+2]; cost on change; funding on longs/shorts.
    Only bars t >= since_ts; a position carried in from before since_ts counts as flat until the
    signal changes side (matches hypothetical_trades / docs)."""
    days: dict[int, list[tuple[float, int]]] = {}
    for rows, opens, fr in bysym.values():
        prev = 0
        carry: Optional[str] = None  # side held into registration; counts as flat until the signal changes
        started = False
        for r in rows:
            t = int(r["asof_ts"])
            if t < since_ts:
                carry = r["side"]
                continue
            if not started:
                if carry is not None and carry != "flat" and r["side"] == carry:
                    continue
                started = True
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
