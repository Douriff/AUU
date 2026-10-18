"""auu_signal shadow tick: non-blocking worker thread, batched writes, skip-unchanged, and a before/after proof.

The "before" side below is the pre-change implementation (``_tick_universe`` + ``record_candidates`` + the autocommit
per-row store writes), copied verbatim from origin/main 13ff38e. The "after" side is the production code. Both are fed
the same fixed synthetic history day by day (new daily bar, forming-bar noise, funding settlements, a history repair)
and every table of the shadow DB must be identical row by row. ``recorded_at`` (write time) is compared separately:
see test_recorded_at_semantics.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from app.marketdata.mainstream.store import MarketStore
from app.paper import auu_signal_shadow as sh
from app.strategies import auu_signal_candidates as C
from app.strategies.auu_signal_v0 import VERSION, AuuSignalV0, align_funding_to_bars

DAY = 86_400_000
EX = "binance"
COINS = ["BTC", "ETH", "SOL", "XRP"]
T0 = C.REG_TS - 150 * DAY  # bar index j covers [T0 + j*DAY, T0 + (j+1)*DAY)
REG_IDX = 150
K0 = REG_IDX + 36  # replay starts 36 bars after registration so closed AND open trades, longs AND shorts exist


# ---------------------------------------------------------------- deterministic synthetic history
def _lcg(seed: int):
    x = seed
    while True:
        x = (1103515245 * x + 12345) % (2**31)
        yield x / 2**31


def _history(coin: str, n: int):
    """n daily bars (regime-switching random walk so that long AND short signals/trades occur) + 8h funding."""
    g = _lcg(sum(ord(c) for c in coin) * 7919)
    px, drift = 100.0 + len(coin), 0.0
    bars = []
    for j in range(n):
        if j % 23 == 0:
            drift = (next(g) - 0.5) * 0.05
        o = px
        px = max(1.0, px * (1 + drift + (next(g) - 0.5) * 0.04))
        v = 1000 + 800 * next(g)
        bars.append([T0 + j * DAY, o, max(o, px) * 1.01, min(o, px) * 0.99, px, v])
    fund = [(T0 + j * DAY + k * 8 * 3_600_000, round((next(g) - 0.45) * 0.0004, 8)) for j in range(n) for k in range(3)]
    return bars, fund


class _Env:
    """Market store with history up to a moving 'now' + a fake service pointing at it."""

    def __init__(self, tmp: str, n_days: int = K0 + 12):
        self.store = MarketStore(Path(tmp) / "mainstream.sqlite")
        self.hist = {c: _history(c, n_days) for c in COINS}
        self.now = 0
        self.svc = SimpleNamespace(
            store=self.store, cfg=SimpleNamespace(strategy_symbols=list(COINS)),
            now_ms=lambda: self.now, exchange_for_read=lambda: EX,
        )

    def advance(self, now: int, *, forming_noise: float = 0.0) -> None:
        """Store content as of ``now``: closed bars final, the bar containing ``now`` partial (forming), funding <= now."""
        self.now = now
        cur = (now - T0) // DAY
        for c in COINS:
            bars, fund = self.hist[c]
            rows = [list(b) for b in bars[: cur + 1]]
            if rows and rows[-1][0] + DAY > now:  # forming bar: partial close/volume, noise changes between ticks
                rows[-1][4] = rows[-1][1] * (1 + 0.003 * (1 + forming_noise))
                rows[-1][5] = rows[-1][5] * 0.3 * (1 + forming_noise)
            self.store.upsert_candles(EX, c, "1d", rows)
            self.store.upsert_funding(EX, c, [f for f in fund if f[0] <= now])

    def close(self) -> None:
        self.store.close()


# ---------------------------------------------------------------- the "before" implementation (verbatim logic)
def _old_upsert_rows(st, symbol, tf, rows, version=VERSION):
    if not rows:
        return 0
    now = st.now_ms()
    data = [(version, symbol, tf, int(r["asof_ts"]), r["side"], float(r["score"]), r["action"],
             json.dumps(r.get("reason_tags") or [], separators=(",", ":")),
             json.dumps(r.get("factors") or {}, separators=(",", ":")), now) for r in rows]
    with st._lock:
        st._db.executemany(
            "INSERT OR REPLACE INTO bars(version,symbol,tf,asof_ts,side,score,action,reason_tags,factors,recorded_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)", data)  # isolation_level=None: one implicit commit PER ROW
        return len(data)


def _old_upsert_cand_bars(st, version, phash, symbol, tf, rows):
    now = st.now_ms()
    data = [(version, phash, symbol, tf, int(r["asof_ts"]), r["side"], float(r["score"]), r["action"],
             json.dumps(r.get("factors") or {}, separators=(",", ":")), now) for r in rows]
    with st._lock:
        st._db.executemany("INSERT OR REPLACE INTO cand_bars VALUES (?,?,?,?,?,?,?,?,?,?)", data)
    return len(data)


def _old_replace_cand_trades(st, version, symbol, trades):
    now = st.now_ms()
    with st._lock:
        st._db.execute("BEGIN")
        st._db.execute("DELETE FROM cand_trades WHERE version=? AND symbol=?", (version, symbol))
        st._db.executemany(
            "INSERT INTO cand_trades VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [(version, symbol, t["side"], t["entry_ts"], t["entry_px"], t["exit_ts"], t["exit_px"],
              t["gross"], t["funding"], t["net"], now) for t in trades])
        st._db.execute("COMMIT")
    return len(trades)


def _old_replace_cand_nav(st, version, rows):
    now = st.now_ms()
    with st._lock:
        st._db.execute("BEGIN")
        st._db.execute("DELETE FROM cand_nav WHERE version=?", (version,))
        st._db.executemany("INSERT INTO cand_nav VALUES (?,?,?,?,?,?,?,?)", [(version, *r, now) for r in rows])
        st._db.execute("COMMIT")
    return len(rows)


def _old_tick_universe(svc, store):
    ex = svc.exchange_for_read()
    for base in svc.cfg.strategy_symbols:
        candles = svc.store.candles(ex, base, "1d", limit=500)
        fr = svc.store.funding(ex, base, limit=500)
        funding_rows = [(int(r["ts"]), float(r["rate"])) for r in fr]
        series = sh.build_series_from_candles(candles, funding_rows=funding_rows, tf="1d", now_ms=svc.now_ms())
        _old_upsert_rows(store, base, "1d", series)
    _old_record_candidates(svc, ex, store)


def _old_record_candidates(svc, ex, store):
    bar_ms = sh.TF_MS["1d"]
    now = svc.now_ms()
    per_version = {}
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
            _old_upsert_cand_bars(store, cid, C.PARAMS_HASH[cid], base, "1d",
                                  [r for r in rows if r["asof_ts"] >= C.REG_TS - 30 * bar_ms])
            per_version.setdefault(cid, {})[base] = (rows, opens, fr)
    for version, bysym in per_version.items():
        for base, (rows, opens, fr) in bysym.items():
            _old_replace_cand_trades(store, version, base,
                                     C.hypothetical_trades(rows, opens, bar_ms=bar_ms, funding_rows=fr, since_ts=C.REG_TS))
        _old_replace_cand_nav(store, version, sh.forward_nav(bysym, bar_ms=bar_ms, since_ts=C.REG_TS))


# ---------------------------------------------------------------- helpers
_TABLES = {
    "bars": "version,symbol,tf,asof_ts",
    "cand_bars": "version,symbol,tf,asof_ts",
    "cand_trades": "version,symbol,entry_ts",
    "cand_nav": "version,ts",
}


def _dump(path: Path, *, drop_recorded_at: bool) -> dict[str, list[tuple]]:
    db = sqlite3.connect(str(path))
    db.row_factory = sqlite3.Row
    out = {}
    for t, pk in _TABLES.items():
        rows = []
        for r in db.execute(f"SELECT * FROM {t} ORDER BY {pk}"):
            d = dict(r)
            if drop_recorded_at:
                d.pop("recorded_at")
            rows.append(tuple(d.items()))
        out[t] = rows
    db.close()
    return out


class _Clock:
    def __init__(self):
        self.t = 1_000

    def __call__(self):
        return self.t


class TickEquivalenceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.env = _Env(self.tmp)
        self.clock_old, self.clock_new = _Clock(), _Clock()
        self.old = sh.SignalShadowStore(Path(self.tmp) / "old.sqlite", now_ms=self.clock_old)
        self.new_path = Path(self.tmp) / "new.sqlite"
        self.new = sh.SignalShadowStore(self.new_path, now_ms=self.clock_new)
        p = mock.patch("app.marketdata.mainstream.get_service", return_value=self.env.svc)
        p.start()
        self.addCleanup(p.stop)
        self.state = sh.TickState()

    def tearDown(self):
        for s in (self.old, self.new):
            s.close()
        self.env.close()
        self._tmp.cleanup()

    def _tick(self, now: int, *, full: bool = True, noise: float = 0.0):
        """Run the old tick and the new tick on the same market data at the same instant."""
        self.env.advance(now, forming_noise=noise)
        self.clock_old.t = self.clock_new.t = now
        _old_tick_universe(self.env.svc, self.old)
        stats = sh._tick_universe(state=self.state, full=full, store=self.new)
        return stats

    def _assert_same(self, label: str):
        a = _dump(Path(self.tmp) / "old.sqlite", drop_recorded_at=True)
        b = _dump(self.new_path, drop_recorded_at=True)
        for t in _TABLES:
            self.assertEqual(a[t], b[t], f"{label}: table {t} differs")
        return a

    def test_first_tick_identical_including_recorded_at(self):
        now = T0 + (K0 + 4) * DAY + 300_000
        self.env.advance(now)
        self.clock_old.t = self.clock_new.t = now
        _old_tick_universe(self.env.svc, self.old)
        sh._tick_universe(state=None, store=self.new)  # state=None: full recompute, like the old tick
        a = _dump(Path(self.tmp) / "old.sqlite", drop_recorded_at=False)
        b = _dump(self.new_path, drop_recorded_at=False)
        for t in _TABLES:
            self.assertEqual(a[t], b[t], f"table {t} differs (recorded_at included, same clock)")
        # not a vacuous comparison
        self.assertGreater(len(a["bars"]), 4 * 100)
        self.assertGreater(len(a["cand_bars"]), 4 * 4 * 30)
        self.assertGreater(len(a["cand_nav"]), 0)
        self.assertGreater(len(a["cand_trades"]), 10)
        sides = {dict(r)["side"] for r in a["cand_trades"]}
        self.assertEqual(sides, {"long", "short"})
        self.assertTrue(any(dict(r)["exit_ts"] is None for r in a["cand_trades"]))  # open trade present too
        self.assertTrue(any(dict(r)["exit_ts"] is not None for r in a["cand_trades"]))  # closed trade present too

    def test_day_by_day_replay_identical_rows_and_trades(self):
        """Old vs new over 6 simulated days, several ticks per day (forming-bar noise, funding settlements, a
        history repair, a restart of the process state). All four tables equal after EVERY tick."""
        nticks = 0
        recomputed_idle = 0
        for k in range(K0 + 1, K0 + 7):
            day0 = T0 + k * DAY
            # 1) just after the daily close: new closed bar
            s = self._tick(day0 + 300_000)
            self._assert_same(f"day {k} close"); nticks += 1
            self.assertGreater(s["rows_written"], 0)  # a new bar must be written
            # 2) forming bar noise only (price/volume of today's partial bar changes): nothing to recompute
            s = self._tick(day0 + 900_000, noise=0.7)
            self._assert_same(f"day {k} +15m"); nticks += 1
            self.assertEqual((s["recomputed"], s["cand_recomputed"], s["rows_written"]), (0, False, 0), s)
            recomputed_idle += s["recomputed"]
            # 3) funding settlement at 08:00 UTC: inputs changed -> recompute allowed, but zero rows may change
            s = self._tick(day0 + 8 * 3_600_000 + 60_000, noise=1.1)
            self._assert_same(f"day {k} +8h"); nticks += 1
            if k == K0 + 3:
                # 4) history repair: an old closed bar is corrected in the market store -> must propagate
                bars, _ = self.env.hist["ETH"]
                bars[REG_IDX - 5][4] *= 1.07
                bars[REG_IDX - 5][5] *= 1.5
                s = self._tick(day0 + 9 * 3_600_000, noise=1.1)
                self._assert_same(f"day {k} repair"); nticks += 1
                self.assertGreater(s["rows_written"], 0)
            if k == K0 + 2:
                # 4b) ONLY an open is corrected (closes/volumes untouched): fills and NAV come from opens, so the
                # fingerprint must cover opens too
                for c in COINS:
                    self.env.hist[c][0][REG_IDX + 25][1] *= 1.03
                s = self._tick(day0 + 9 * 3_600_000, noise=1.1)
                self._assert_same(f"day {k} open-only repair"); nticks += 1
                self.assertGreater(s["rows_written"], 0)
            if k == K0 + 4:
                self.state = sh.TickState()  # process restart: caches empty, DB has everything
                s = self._tick(day0 + 10 * 3_600_000, noise=1.3)
                self._assert_same(f"day {k} restart"); nticks += 1
                self.assertEqual(s["rows_written"], 0, "restart on an up-to-date DB must not rewrite anything")
        self.assertGreaterEqual(nticks, 18)
        self.assertEqual(recomputed_idle, 0)

    def test_probe_only_tick_processes_new_bar_and_idle_probe_touches_nothing(self):
        now = T0 + (K0 + 2) * DAY + 300_000
        self._tick(now)
        # idle probes: no shadow DB connection at all, no recompute
        opened = []
        real = sh.SignalShadowStore

        def spy(*a, **kw):
            opened.append(1)
            return real(*a, **kw)

        with mock.patch.object(sh, "SignalShadowStore", spy), mock.patch.object(
            sh, "build_series_from_candles", side_effect=AssertionError("recompute on idle probe")
        ):
            self.env.advance(now + 120_000, forming_noise=0.5)
            r = sh._tick_universe(state=self.state, full=False)  # own store would be opened if it got that far
        self.assertEqual(r, {"skipped": "probe_unchanged", "full": False})
        self.assertEqual(opened, [])
        # the next daily bar closes: a probe-only tick (full=False) must pick it up
        nxt = T0 + (K0 + 3) * DAY + 200_000
        self.env.advance(nxt)
        self.clock_new.t = nxt
        r = sh._tick_universe(state=self.state, full=False, store=self.new)
        self.assertEqual(r.get("skipped"), None)
        self.assertEqual(r["recomputed"], len(COINS))
        self.assertTrue(r["cand_recomputed"])
        last = self.new.latest("BTC", "1d")
        self.assertEqual(last["asof_ts"], T0 + (K0 + 2) * DAY)  # newest closed bar

    def test_force_and_inode_change_rewrite_missing_rows(self):
        now = T0 + (K0 + 2) * DAY + 300_000
        self._tick(now)
        with self.new._lock:
            self.new._db.execute("DELETE FROM bars WHERE symbol='SOL'")
            self.new._db.execute("DELETE FROM cand_nav")
        # state still says "unchanged" -> an ordinary full tick does not notice (by design) ...
        self.env.advance(now + 600_000)
        sh._tick_universe(state=self.state, full=True, store=self.new)
        self.assertEqual(self.new.count("SOL"), 0)
        # ... but the periodic forced pass (and a replaced DB file) self-heals
        self.state.last_force_mono = time.monotonic() - 10 * 86400
        sh._tick_universe(state=self.state, full=True, store=self.new)
        self._assert_same_after_old_refresh(now + 600_000)

    def _assert_same_after_old_refresh(self, now):
        self.clock_old.t = now
        _old_tick_universe(self.env.svc, self.old)
        self._assert_same("after forced pass")

    def test_recorded_at_semantics(self):
        """Unchanged rows keep their first-record time; the newest bar per symbol carries the liveness heartbeat."""
        t1 = T0 + (K0 + 2) * DAY + 300_000
        self._tick(t1)
        t2 = T0 + (K0 + 3) * DAY + 300_000
        self._tick(t2)
        db = sqlite3.connect(str(self.new_path))
        rows = db.execute("SELECT asof_ts, recorded_at FROM bars WHERE symbol='BTC' ORDER BY asof_ts").fetchall()
        newest = rows[-1]
        self.assertEqual(newest[1], t2)  # heartbeat
        # (rows near the start of the 500-row funding window legitimately re-derive when that window slides by one
        # settlement; that is existing behaviour and the old tick rewrites them too: equality is asserted elsewhere)
        self.assertTrue(all(r[1] == t1 for r in rows[-60:-2]), "recent history rows must keep first-record time")
        self.assertLessEqual(sum(1 for r in rows[:-2] if r[1] != t1), 3)
        self.assertEqual(db.execute("SELECT value FROM meta WHERE key='last_tick_ms'").fetchone()[0], str(t2))
        # the old implementation re-stamps EVERY row on every tick
        dbo = sqlite3.connect(str(Path(self.tmp) / "old.sqlite"))
        self.assertEqual({r[0] for r in dbo.execute("SELECT recorded_at FROM bars WHERE symbol='BTC'")}, {t2})
        db.close(); dbo.close()


class StoreWriteTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.st = sh.SignalShadowStore(Path(self._tmp.name) / "s.sqlite")
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self.st.close)

    @staticmethod
    def _rows(n, t0=1_700_000_000_000, off=0.0):
        return [{"asof_ts": t0 + i * DAY, "side": "long", "score": 0.1 * (i % 7) + off, "action": "none",
                 "reason_tags": ["a"], "factors": {"momentum": 0.5, "funding": None}} for i in range(n)]

    def _trace(self):
        stm = []
        self.st._db.set_trace_callback(lambda s: stm.append(s.split("(")[0].strip().upper()[:24]))
        return stm

    def test_batch_is_one_transaction(self):
        stm = self._trace()
        self.assertEqual(self.st.upsert_rows("BTC", "1d", self._rows(500)), 500)
        self.assertEqual(stm.count("BEGIN IMMEDIATE"), 1)
        self.assertEqual(stm.count("COMMIT"), 1)
        stm.clear()
        self.assertEqual(self.st.upsert_cand_bars("cand_A", "h", "BTC", "1d", self._rows(300)), 300)
        self.assertEqual((stm.count("BEGIN IMMEDIATE"), stm.count("COMMIT")), (1, 1))
        # the trace shows no statement outside a BEGIN..COMMIT pair
        self.assertEqual(stm[0], "BEGIN IMMEDIATE")
        self.assertEqual(stm[-1], "COMMIT")

    def test_default_upsert_rewrites_all_but_skip_unchanged_does_not(self):
        rows = self._rows(50)
        self.st.now_ms = lambda: 111
        self.st.upsert_rows("BTC", "1d", rows)
        self.st.now_ms = lambda: 222
        self.assertEqual(self.st.upsert_rows("BTC", "1d", rows), 50)  # legacy semantics: all rewritten
        self.assertEqual({r["recorded_at"] for r in self.st.series("BTC", limit=100)}, {222})
        self.st.now_ms = lambda: 333
        changed = self._rows(50)
        changed[10]["score"] = 0.999
        self.assertEqual(self.st.upsert_rows("BTC", "1d", changed, skip_unchanged=True), 1)
        got = {r["asof_ts"]: r for r in self.st.series("BTC", limit=100)}
        self.assertEqual(got[changed[10]["asof_ts"]]["recorded_at"], 333)
        self.assertEqual(got[changed[10]["asof_ts"]]["score"], 0.999)
        self.assertEqual(got[changed[11]["asof_ts"]]["recorded_at"], 222)

    def test_unchanged_batch_opens_no_transaction(self):
        rows = self._rows(40)
        self.st.upsert_rows("BTC", "1d", rows)
        self.st.upsert_cand_bars("cand_A", "h", "BTC", "1d", rows)
        stm = self._trace()
        self.assertEqual(self.st.upsert_rows("BTC", "1d", rows, skip_unchanged=True), 0)
        self.assertEqual(self.st.upsert_cand_bars("cand_A", "h", "BTC", "1d", rows, skip_unchanged=True), 0)
        self.assertNotIn("BEGIN IMMEDIATE", stm)  # no write transaction => no commit, no fsync
        self.assertNotIn("COMMIT", stm)

    def test_failed_batch_rolls_back_and_connection_stays_usable(self):
        good = [dict(side="long", entry_ts=1, entry_px=1.0, exit_ts=2, exit_px=2.0, gross=1.0, funding=0.0, net=0.9)]
        self.st.replace_cand_trades("cand_A", "BTC", good)
        dup = good + [dict(good[0])]  # same PK -> IntegrityError mid-batch
        with self.assertRaises(sqlite3.IntegrityError):
            self.st.replace_cand_trades("cand_A", "BTC", dup)
        n = self.st._db.execute("SELECT COUNT(*) FROM cand_trades").fetchone()[0]
        self.assertEqual(n, 1)  # atomic: old rows still there
        self.assertFalse(self.st._db.in_transaction)
        self.st.replace_cand_trades("cand_A", "BTC", [])  # connection still fine
        self.assertEqual(self.st._db.execute("SELECT COUNT(*) FROM cand_trades").fetchone()[0], 0)

    def test_trades_and_nav_skip_when_equal(self):
        tr = [dict(side="long", entry_ts=5, entry_px=1.0, exit_ts=None, exit_px=None, gross=None, funding=None, net=None)]
        self.st.now_ms = lambda: 1
        self.st.replace_cand_trades("v", "BTC", tr, skip_unchanged=True)
        self.st.now_ms = lambda: 2
        c0 = self.st.stats["commits"]
        self.st.replace_cand_trades("v", "BTC", tr, skip_unchanged=True)
        self.assertEqual(self.st.stats["commits"], c0)
        self.assertEqual(self.st._db.execute("SELECT recorded_at FROM cand_trades").fetchone()[0], 1)
        nav = [(10, 0.01, 1.01, 1, 0, 1)]
        self.st.replace_cand_nav("v", nav, skip_unchanged=True)
        c1 = self.st.stats["commits"]
        self.st.replace_cand_nav("v", nav, skip_unchanged=True)
        self.assertEqual(self.st.stats["commits"], c1)
        self.st.replace_cand_nav("v", [(10, 0.01, 1.01, 1, 0, 1), (20, 0.02, 1.03, 1, 0, 1)], skip_unchanged=True)
        self.assertEqual(self.st._db.execute("SELECT COUNT(*) FROM cand_nav").fetchone()[0], 2)


class LoopTests(unittest.TestCase):
    def test_intervals_env_and_clamp(self):
        with mock.patch.dict("os.environ", {}, clear=False):
            for k in ("AUU_SIGNAL_SHADOW_INTERVAL_SEC", "AUU_SIGNAL_SHADOW_PROBE_SEC"):
                __import__("os").environ.pop(k, None)
            self.assertEqual(sh.tick_intervals(), (600, 120))
        with mock.patch.dict("os.environ", {"AUU_SIGNAL_SHADOW_INTERVAL_SEC": "99999", "AUU_SIGNAL_SHADOW_PROBE_SEC": "5"}):
            self.assertEqual(sh.tick_intervals(), (1800, 30))
        with mock.patch.dict("os.environ", {"AUU_SIGNAL_SHADOW_INTERVAL_SEC": "10", "AUU_SIGNAL_SHADOW_PROBE_SEC": "bad"}):
            self.assertEqual(sh.tick_intervals(), (60, 60))

    def test_run_loop_never_blocks_event_loop_and_runs_tick_in_worker_thread(self):
        main = threading.get_ident()
        seen = []

        def slow_tick(**kw):
            seen.append(threading.get_ident())
            time.sleep(0.6)  # stand-in for 8-30 s of synchronous work
            return {}

        async def go():
            lag = [0.0]

            async def probe():
                while True:
                    t = time.perf_counter()
                    await asyncio.sleep(0.01)
                    lag[0] = max(lag[0], time.perf_counter() - t - 0.01)

            pt = asyncio.create_task(probe())
            lt = asyncio.create_task(sh.run_loop(interval_sec=1, probe_sec=1))
            await asyncio.sleep(0.9)
            lt.cancel()
            pt.cancel()
            for t in (lt, pt):
                with self.assertRaises(asyncio.CancelledError):
                    await t
            return lag[0]

        with mock.patch.object(sh, "_tick_universe", slow_tick), mock.patch.object(sh, "shadow_enabled", lambda: True):
            lag = asyncio.run(go())
        self.assertTrue(seen)
        self.assertNotIn(main, seen)
        self.assertLess(lag, 0.25, f"event loop stalled {lag:.3f}s while the tick ran")

    def test_tick_opens_and_closes_its_own_connection_inside_the_worker_thread(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = _Env(tmp)
            env.advance(T0 + (K0 + 3) * DAY + 300_000)
            threads = {}
            real = sh.SignalShadowStore

            class Spy(real):
                def __init__(self, *a, **kw):
                    threads["open"] = threading.get_ident()
                    super().__init__(*a, **kw)

                def close(self):
                    threads["close"] = threading.get_ident()
                    super().close()

            async def go():
                await asyncio.to_thread(sh._tick_universe, state=sh.TickState(), full=True)
                return threading.get_ident()

            with mock.patch.dict("os.environ", {"AUU_DATA_DIR": tmp}), mock.patch(
                "app.marketdata.mainstream.get_service", return_value=env.svc
            ), mock.patch.object(sh, "SignalShadowStore", Spy):
                main = asyncio.run(go())
            self.assertEqual(threads["open"], threads["close"])
            self.assertNotEqual(threads["open"], main)
            env.close()


if __name__ == "__main__":
    unittest.main()
