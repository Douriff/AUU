"""M3: daily paper runner for the mainstream strategy (system paper ledger).

Once per UTC day, after the daily bar of day D has closed and the market store holds the
final values, the runner:

1. marks yesterday's weights to day D (close-to-close return minus funding for D),
2. calls the strategy's ``decide()`` on the panel ending at D (same code as the backtest),
3. trades to the new targets at D's close with the backtest's band rule and cost model
   (taker fee + per-coin slippage), via :func:`app.backtest.engine.step`.

The result is written to the system mainstream paper ledger
``data_dir()/mainstream_strategy.sqlite`` in one transaction per day; ``day`` is the primary key, so a
restart, a second process, or a re-run of a day can never trade twice. Missed days
(downtime) are replayed in order on the next tick (flagged ``catchup``) because every day
only depends on data up to that day. Paper only: nothing here can reach an exchange order API.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Optional

from app.backtest import stats
from app.backtest.costs import CostModel
from app.backtest.engine import step
from app.backtest.panel import DAY_MS, Panel, ms_day
from app.data_paths import data_dir
from app.paper import strategy_risk as risk
from app.paper.strategy_risk import HOUR_MS, RiskLimits
from app.strategies.trend_tsmom import TrendTSMOM

log = logging.getLogger("auu.strategy")

STALL_HOURS = 26.0
GO_MIN_DAYS = 250
FUNDING_READY_MS = 16 * 3_600_000  # last 8h settlement of day D is at D 16:00 UTC
BAND = 0.2
MIN_TRADE = 0.01

PanelFn = Callable[[int], Optional[Panel]]  # day_ms -> panel whose last row is that day
ReadyFn = Callable[[int], tuple[bool, str]]  # day_ms -> (final data present?, reason)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS runs (
  day INTEGER PRIMARY KEY,           -- UTC day (ms) whose close the rebalance used
  strategy TEXT NOT NULL,
  ran_at INTEGER NOT NULL,           -- wall clock (ms)
  catchup INTEGER NOT NULL DEFAULT 0,
  exchange TEXT, source TEXT,
  nav_open REAL NOT NULL, pnl REAL NOT NULL, funding REAL NOT NULL, cost REAL NOT NULL,
  ret REAL NOT NULL, nav_close REAL NOT NULL,
  gross_before REAL NOT NULL, gross_after REAL NOT NULL, turnover REAL NOT NULL,
  weights TEXT NOT NULL, targets TEXT NOT NULL, closes TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS universe_changes (
  day INTEGER PRIMARY KEY,           -- first rebalance day that used the new universe
  coins TEXT NOT NULL, prev TEXT NOT NULL, recorded_at INTEGER NOT NULL, note TEXT
);
CREATE TABLE IF NOT EXISTS fills (
  day INTEGER NOT NULL, coin TEXT NOT NULL, side TEXT NOT NULL,
  w_from REAL NOT NULL, w_to REAL NOT NULL, notional REAL NOT NULL, qty REAL NOT NULL,
  price REAL NOT NULL, fill_price REAL NOT NULL, fee REAL NOT NULL, slippage REAL NOT NULL,
  PRIMARY KEY (day, coin)
);
-- risk caps (leverage report §3.2): every trigger, the intraday adjustments it caused, and the caps' state
CREATE TABLE IF NOT EXISTS risk_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, day INTEGER, kind TEXT NOT NULL, action TEXT NOT NULL,
  value REAL, threshold REAL, detail TEXT, at TEXT NOT NULL      -- at: 'close' (daily rebalance) or 'intraday'
);
CREATE TABLE IF NOT EXISTS adjustments (
  ts INTEGER PRIMARY KEY,            -- close of the 1h bar the adjustment traded at
  day INTEGER NOT NULL,              -- last rebalance day it follows
  kinds TEXT NOT NULL, nav_ref REAL NOT NULL, nav_mark REAL NOT NULL, nav_after REAL NOT NULL,
  pnl_usd REAL NOT NULL, funding_usd REAL NOT NULL, cost_usd REAL NOT NULL, turnover REAL NOT NULL,
  weights_before TEXT NOT NULL, weights_after TEXT NOT NULL, prices TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS risk_fills (
  ts INTEGER NOT NULL, coin TEXT NOT NULL, side TEXT NOT NULL, w_from REAL NOT NULL, w_to REAL NOT NULL,
  notional REAL NOT NULL, qty REAL NOT NULL, price REAL NOT NULL, fill_price REAL NOT NULL, fee REAL NOT NULL, slippage REAL NOT NULL,
  PRIMARY KEY (ts, coin)
);
CREATE TABLE IF NOT EXISTS risk_state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "") or default)
    except ValueError:
        return default


def runner_enabled() -> bool:
    return os.getenv("AUU_STRATEGY_RUNNER", "on").strip().lower() not in {"0", "false", "off", "no"}


class StrategyLedger:
    """SQLite system paper ledger for the strategy (one row per rebalanced day)."""

    def __init__(self, path: Optional[Path | str] = None, *, now_ms: Callable[[], int] = lambda: int(time.time() * 1000)):
        self.path = Path(path) if path else data_dir() / "mainstream_strategy.sqlite"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None, timeout=30)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(_SCHEMA)
        cols = {r[1] for r in self._db.execute("PRAGMA table_info(runs)")}
        if "universe" not in cols:  # ledgers created before the universe column (history stays as written)
            self._db.execute("ALTER TABLE runs ADD COLUMN universe TEXT")
        if "risk_targets" not in cols:  # targets after the risk caps (NULL = caps did not change them)
            self._db.execute("ALTER TABLE runs ADD COLUMN risk_targets TEXT")
        self._lock = threading.RLock()
        # First time this ledger was ever opened: the stall clock starts here (survives restarts).
        self._db.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('created_at', ?)", (str(now_ms()),))

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def meta(self, key: str) -> Optional[str]:
        row = self._db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def last_run(self) -> Optional[sqlite3.Row]:
        with self._lock:
            return self._db.execute("SELECT * FROM runs ORDER BY day DESC LIMIT 1").fetchone()

    def runs(self) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute("SELECT * FROM runs ORDER BY day").fetchall()

    def fills(self, limit: int = 50) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute("SELECT * FROM fills ORDER BY day DESC, coin LIMIT ?", (limit,)).fetchall()

    def universe_changes(self) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute("SELECT * FROM universe_changes ORDER BY day").fetchall()

    # ---- risk caps ---------------------------------------------------------------
    def risk_get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            row = self._db.execute("SELECT value FROM risk_state WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def risk_events(self, limit: int = 50) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute("SELECT * FROM risk_events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

    def adjustments(self, limit: int = 1000) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute("SELECT * FROM adjustments ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()

    def last_adjustment(self, after_day: int) -> Optional[sqlite3.Row]:
        with self._lock:
            return self._db.execute("SELECT * FROM adjustments WHERE day=? ORDER BY ts DESC LIMIT 1", (after_day,)).fetchone()

    def day_adjustments(self, after_day: int) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute("SELECT * FROM adjustments WHERE day=? ORDER BY ts", (after_day,)).fetchall()

    def peak_nav(self, start_nav: float) -> float:
        with self._lock:
            a = self._db.execute("SELECT MAX(nav_close) FROM runs").fetchone()[0]
            b = self._db.execute("SELECT MAX(nav_after) FROM adjustments").fetchone()[0]
        return max(x for x in (start_nav, a, b) if x is not None)

    @staticmethod
    def _write_risk(db, events: list[dict], state: dict) -> None:
        for e in events:
            db.execute("INSERT INTO risk_events(ts, day, kind, action, value, threshold, detail, at) VALUES (?,?,?,?,?,?,?,?)",
                       (e["ts"], e.get("day"), e["kind"], e["action"], e.get("value"), e.get("threshold"),
                        json.dumps(e.get("detail") or {}), e.get("at", "close")))
        for k, v in state.items():
            if v is None:
                db.execute("DELETE FROM risk_state WHERE key=?", (k,))
            else:
                db.execute("INSERT OR REPLACE INTO risk_state(key, value) VALUES (?, ?)", (k, json.dumps(v)))

    def commit_risk(self, events: list[dict], state: dict, *, adjustment: Optional[dict] = None,
                    fills: Optional[list[dict]] = None, expect_day: Optional[int] = None) -> bool:
        """Write events/state (and an intraday adjustment) atomically; False if the ledger moved on."""
        with self._lock:
            db = self._db
            db.execute("BEGIN IMMEDIATE")
            try:
                if expect_day is not None and db.execute("SELECT MAX(day) AS d FROM runs").fetchone()["d"] != expect_day:
                    db.execute("ROLLBACK")
                    return False
                if adjustment is not None:
                    if db.execute("SELECT 1 FROM adjustments WHERE ts=?", (adjustment["ts"],)).fetchone():
                        db.execute("ROLLBACK")
                        return False
                    db.execute(f"INSERT INTO adjustments({','.join(adjustment)}) VALUES ({','.join('?' * len(adjustment))})",
                               tuple(adjustment.values()))
                    for f in fills or []:
                        db.execute(f"INSERT INTO risk_fills({','.join(f)}) VALUES ({','.join('?' * len(f))})", tuple(f.values()))
                self._write_risk(db, events, state)
                db.execute("COMMIT")
                return True
            except Exception:
                db.execute("ROLLBACK")
                raise

    def commit_day(self, run: dict, fills: list[dict], *, expect_prev: Optional[int], meta: dict[str, str],
                   universe_change: Optional[dict] = None, risk_events: Optional[list[dict]] = None,
                   risk_state: Optional[dict] = None) -> bool:
        """Write one day atomically. False (nothing written) if the day exists or the ledger moved on."""
        with self._lock:
            db = self._db
            db.execute("BEGIN IMMEDIATE")
            try:
                cur = db.execute("SELECT MAX(day) AS d FROM runs").fetchone()["d"]
                if cur != expect_prev or db.execute("SELECT 1 FROM runs WHERE day=?", (run["day"],)).fetchone():
                    db.execute("ROLLBACK")
                    return False
                cols = ",".join(run)
                db.execute(f"INSERT INTO runs({cols}) VALUES ({','.join('?' * len(run))})", tuple(run.values()))
                for f in fills:
                    db.execute(f"INSERT INTO fills({','.join(f)}) VALUES ({','.join('?' * len(f))})", tuple(f.values()))
                for k, v in meta.items():
                    db.execute("INSERT OR IGNORE INTO meta(key, value) VALUES (?, ?)", (k, v))
                if universe_change:
                    db.execute("INSERT INTO universe_changes(day, coins, prev, recorded_at, note) VALUES (?,?,?,?,?)",
                               (universe_change["day"], universe_change["coins"], universe_change["prev"],
                                universe_change["recorded_at"], universe_change.get("note")))
                self._write_risk(db, risk_events or [], risk_state or {})
                db.execute("COMMIT")
                return True
            except Exception:
                db.execute("ROLLBACK")
                raise


@contextmanager
def _file_lock(path: Path):
    """Non-blocking inter-process lock; yields False when another process holds it."""
    try:
        import fcntl
    except ImportError:  # pragma: no cover - non-POSIX
        yield True
        return
    fh = open(path, "a+")
    try:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)
    finally:
        fh.close()


class StrategyRunner:
    def __init__(
        self,
        ledger: StrategyLedger,
        *,
        panel_fn: PanelFn,
        ready_fn: ReadyFn,
        strategy: Optional[TrendTSMOM] = None,
        cost: Optional[CostModel] = None,
        start_nav: Optional[float] = None,
        max_catchup_days: int = 30,
        exchange_fn: Callable[[], Optional[str]] = lambda: None,
        universe_fn: Callable[[], dict] = lambda: {},
        now_ms: Callable[[], int] = lambda: int(time.time() * 1000),
        risk_limits: Optional[RiskLimits] = None,
        marks_fn: Optional[Callable[[list[str], int], dict]] = None,
        funding_fn: Optional[Callable[[str, int, int], list[tuple[int, float]]]] = None,
        on_commit: Optional[Callable[[int, list[dict], bool], None]] = None,
    ):
        """``risk_limits`` None = caps off. ``marks_fn(coins, bar_ts)`` -> {"prices": {coin: (bar_ts, close)}, "ok", "reason"}
        gives 1h closes for the intraday monitor; ``funding_fn(coin, lo, hi)`` -> settlements with lo <= ts < hi."""
        self.risk = risk_limits
        self.marks_fn, self.funding_fn = marks_fn, funding_fn
        self.on_commit = on_commit  # observers only (exec-price shadow); never feeds back into the ledger
        self.universe_fn = universe_fn
        self.ledger = ledger
        self.panel_fn, self.ready_fn, self.exchange_fn = panel_fn, ready_fn, exchange_fn
        self.strategy = strategy or TrendTSMOM()
        self.cost = cost or CostModel()
        self.start_nav = float(start_nav if start_nav is not None else _env_float("AUU_STRATEGY_START_USDT", 10_000.0))
        self.max_catchup_days = max_catchup_days
        self.now_ms = now_ms
        self.waiting = ""
        self.last_error = ""
        self.last_tick_ms = 0
        self._tick_lock = threading.Lock()
        self._go_cache: tuple[int, dict] | None = None

    # ---- schedule ------------------------------------------------------------------
    def due_day(self) -> int:
        """Newest UTC day whose daily bar has closed."""
        return self.now_ms() // DAY_MS * DAY_MS - DAY_MS

    def tick(self) -> list[int]:
        """Run every due day that is not in the ledger yet. Returns the days written."""
        if not self._tick_lock.acquire(blocking=False):
            return []
        try:
            with _file_lock(self.ledger.path.with_suffix(".lock")) as got:
                if not got:
                    self.waiting = "another process is running the strategy"
                    return []
                out = self._tick()
                if self.risk is not None and self.marks_fn is not None:
                    try:
                        self.monitor()
                    except Exception as exc:  # the daily ledger must not depend on the monitor
                        self.last_error = f"risk monitor {type(exc).__name__}: {exc}"[:300]
                        log.exception("strategy risk monitor failed")
                return out
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"[:300]
            log.exception("strategy tick failed")
            return []
        finally:
            self.last_tick_ms = self.now_ms()
            self._tick_lock.release()

    def _tick(self) -> list[int]:
        due = self.due_day()
        last = self.ledger.last_run()
        last_day = int(last["day"]) if last else None
        if last_day is not None and last_day >= due:
            self.waiting = ""
            return []
        days = [due] if last_day is None else list(range(last_day + DAY_MS, due + 1, DAY_MS))
        if len(days) > self.max_catchup_days:
            self.waiting = f"missed {len(days)} days (> {self.max_catchup_days}); manual review needed before replaying"
            return []
        ok, reason = self.ready_fn(due)
        if not ok:
            self.waiting = f"waiting for {ms_day(due)} data: {reason}"
            return []
        panel = self.panel_fn(due)
        if panel is None or not len(panel) or panel.days[-1] != due:
            self.waiting = f"panel for {ms_day(due)} not available"
            return []
        written = []
        for d in days:
            if d not in panel.days:
                self.waiting = f"panel has no row for {ms_day(d)}"
                break
            if not self._run_day(panel, panel.days.index(d), catchup=d != due):
                break  # someone else wrote it; re-read on the next tick
            written.append(d)
        self.waiting = ""
        self.last_error = ""
        return written

    # ---- one day -----------------------------------------------------------------
    def _run_day(self, panel: Panel, i: int, *, catchup: bool) -> bool:
        day = panel.days[i]
        last = self.ledger.last_run()
        coins = list(panel.coins)
        prev_w = json.loads(last["weights"]) if last else {}
        nav_open = float(last["nav_close"]) if last else self.start_nav
        # Coins dropped from the universe since yesterday: exit at 0 return (no price).
        for c in prev_w:
            if c not in coins:
                coins.append(c)

        def close(c: str, j: int) -> Optional[float]:
            s = panel.perp_close.get(c)
            return s[j] if s is not None and 0 <= j < len(s) else None

        # An intraday risk adjustment since the last close splits the day: the last segment starts
        # at the adjustment's NAV/weights/prices; earlier segments are already booked in `adjustments`.
        adj = self.ledger.last_adjustment(int(last["day"])) if (last is not None and self.risk is not None) else None
        nav_seg = float(adj["nav_after"]) if adj else nav_open
        ref_px = json.loads(adj["prices"]) if adj else {}
        if adj:
            prev_w = json.loads(adj["weights_after"])
        r, f = [], []
        for c in coins:
            p1, p0 = close(c, i), (ref_px.get(c) if adj else close(c, i - 1))
            r.append(p1 / p0 - 1.0 if (p1 is not None and p0 not in (None, 0)) else 0.0)
            if adj and self.funding_fn is not None:
                f.append(sum(x for _, x in self.funding_fn(c, int(adj["ts"]) + 1, day + DAY_MS)))
            else:
                fs = panel.funding.get(c)
                f.append(float(fs[i]) if fs is not None else 0.0)
        targets = self.strategy.decide(panel.truncate(i + 1))
        tg = [float(targets.get(c, 0.0) or 0.0) for c in coins]
        w0 = [float(prev_w.get(c, 0.0)) for c in coins]
        cps = [self.cost.per_side(c) for c in coins]
        tg_used, r_events, r_state = tg, [], {}
        if self.risk is not None:
            tg_used, r_events, r_state = self._risk_targets(coins, tg, w0, r, f, nav_seg, nav_open, day)
        st = step(w0, r, f, tg_used, cps, band=BAND, min_trade=MIN_TRADE)
        if self.risk is not None:
            st = self._hard_cap(st, coins, cps, r_events)
        nav_trade = nav_seg * (1.0 + st.pnl)
        nav_close = nav_seg * (1.0 + st.ret)
        pnl, fund_frac, cost_frac, turnover = st.pnl, st.funding, st.cost, st.turnover
        if adj:  # whole-day figures as fractions of the day's opening NAV
            adjs = self.ledger.day_adjustments(int(last["day"]))
            cost_usd = sum(float(a["cost_usd"]) for a in adjs) + st.cost * nav_trade
            fund_usd = sum(float(a["funding_usd"]) for a in adjs) + st.funding * nav_seg
            turnover += sum(float(a["turnover"]) for a in adjs)
            cost_frac, fund_frac = cost_usd / nav_open, fund_usd / nav_open
            pnl = (nav_close / nav_open - 1.0) + cost_frac
        ret = nav_close / nav_open - 1.0 if adj else st.ret
        fills = []
        for k, w_from, w_to in st.trades:
            c = coins[k]
            px = close(c, i)
            if px is None:
                continue
            notional = abs(w_to - w_from) * nav_trade
            side = "buy" if w_to > w_from else "sell"
            slip = self.cost.slippage.get(c, self.cost.slippage_default) * self.cost.mult
            fills.append({
                "day": day, "coin": c, "side": side, "w_from": w_from, "w_to": w_to,
                "notional": notional, "qty": notional / px, "price": px,
                "fill_price": px * (1 + slip if side == "buy" else 1 - slip),
                "fee": notional * self.cost.taker * self.cost.mult, "slippage": notional * slip,
            })
        weights = {c: w for c, w in zip(coins, st.weights) if w != 0.0}
        universe = list(panel.coins)
        change = None
        if last is not None:
            prev_u = json.loads(last["universe"]) if last["universe"] else list(json.loads(last["targets"]))
            if set(prev_u) != set(universe):
                change = {"day": day, "coins": json.dumps(universe), "prev": json.dumps(prev_u), "recorded_at": self.now_ms(),
                          "note": f"universe {len(prev_u)} -> {len(universe)} coins from the {ms_day(day)} close; earlier days unchanged"}
        run = {
            "day": day, "strategy": self.strategy.name, "ran_at": self.now_ms(), "catchup": int(catchup),
            "exchange": self.exchange_fn(), "source": panel.source,
            "nav_open": nav_open, "pnl": pnl, "funding": fund_frac, "cost": cost_frac, "ret": ret,
            "nav_close": nav_close, "gross_before": st.gross_held, "gross_after": sum(abs(x) for x in st.weights),
            "turnover": turnover, "weights": json.dumps(weights), "targets": json.dumps({c: t for c, t in zip(coins, tg)}),
            "closes": json.dumps({c: close(c, i) for c in coins}),
            "universe": json.dumps(universe),
            "risk_targets": json.dumps({c: t for c, t in zip(coins, tg_used)}) if tg_used != tg else None,
        }
        meta = {"strategy": self.strategy.name, "params": json.dumps(self.strategy.describe()),
                "start_day": str(day), "start_nav": str(self.start_nav)}
        for e in r_events:
            e.update(ts=day + DAY_MS, day=day, at="close")
        ok = self.ledger.commit_day(run, fills, expect_prev=int(last["day"]) if last else None, meta=meta, universe_change=change,
                                    risk_events=r_events, risk_state=r_state)
        if ok:
            log.info("strategy %s rebalanced %s nav=%.2f ret=%.5f fills=%d%s", self.strategy.name, ms_day(day), nav_close, ret, len(fills), " (catch-up)" if catchup else "")
            for e in r_events:
                log.warning("RISK %s %s at %s close: value=%s threshold=%s %s", e["kind"], e["action"], ms_day(day), e.get("value"), e.get("threshold"), e.get("detail"))
            if self.on_commit is not None:
                try:
                    self.on_commit(day, [dict(f) for f in fills], catchup)
                except Exception:
                    log.exception("on_commit observer failed (ledger unaffected)")
        return ok

    # ---- risk caps -----------------------------------------------------------------
    def _hard_cap(self, st, coins, cps, events):
        """The band can leave a held coin above 25% (or the book above 1x): trade those down at the same close."""
        capped, hits = risk.cap_weights(list(st.weights), self.risk)
        if not hits:
            return st
        extra = step(list(st.weights), [0.0] * len(coins), [0.0] * len(coins), capped, cps, band=0.0, min_trade=0.0)
        trades = {k: (a, b) for k, a, b in st.trades}
        for k, a, b in extra.trades:
            trades[k] = (trades[k][0] if k in trades else a, b)
        events.append({"kind": "cap_coin" if "coin" in hits else "cap_gross", "action": "trade_down_held",
                       "value": max(abs(x) for x in st.weights) if "coin" in hits else sum(abs(x) for x in st.weights),
                       "threshold": self.risk.max_coin if "coin" in hits else self.risk.max_gross,
                       "detail": {coins[k]: round(b, 4) for k, _, b in extra.trades}})
        from dataclasses import replace

        return replace(st, weights=extra.weights, cost=st.cost + extra.cost, turnover=st.turnover + extra.turnover,
                       trades=[(k, a, b) for k, (a, b) in sorted(trades.items())])

    def _f8(self, c: str, at: int) -> Optional[tuple[int, float]]:
        if self.funding_fn is None:
            return None
        return risk.f8_of(self.funding_fn(c, at - 3 * DAY_MS, at + 1))

    def _locked(self, at: int) -> bool:
        lk = self.ledger.risk_get("lock")
        return bool(lk) and (lk.get("kind") == "review" or int(lk.get("until", 0)) > at)

    def _risk_targets(self, coins, tg, w0, r, f, nav_seg, nav_open, day):
        fund = sum(wi * fi for wi, fi in zip(w0, f))
        pnl = sum(wi * ri for wi, ri in zip(w0, r)) - fund
        g = 1.0 + pnl
        drifted = [wi * (1.0 + ri) / g for wi, ri in zip(w0, r)] if g > 0 else [0.0] * len(w0)
        nav_pre = nav_seg * g
        close_ts = day + DAY_MS
        peak = self.ledger.peak_nav(self.start_nav)
        f8 = {c: (x[1] if x else None) for c in coins for x in [self._f8(c, close_ts - 1)]}  # settlements of day D
        flags = self.ledger.risk_get(f"day:{day}", {}) or {}
        out, ev, st = risk.rebalance_targets(
            coins, tg, drifted, lim=self.risk, locked=self._locked(close_ts), day_ret=nav_pre / nav_open - 1.0,
            dd=nav_pre / peak - 1.0, day_flags=flags, data_bad=bool(self.ledger.risk_get("data_bad")), f8=f8)
        state: dict = {}
        if st.get("lock") == "review":
            state["lock"] = {"kind": "review", "since": close_ts, "reason": "drawdown <= %.0f%%" % (self.risk.dd_flat * 100)}
        elif st.get("lock") == "24h":
            state["lock"] = {"kind": "24h", "since": close_ts, "until": close_ts + int(self.risk.day_lock_hours * HOUR_MS),
                             "reason": "day loss <= %.0f%%" % (self.risk.day_flat * 100)}
        return out, ev, state

    def _ref_state(self, last) -> tuple[float, dict, dict, int]:
        """(nav, weights, prices, funding-from ts) the positions were last booked at."""
        adj = self.ledger.last_adjustment(int(last["day"]))
        if adj:
            return float(adj["nav_after"]), json.loads(adj["weights_after"]), json.loads(adj["prices"]), int(adj["ts"]) + 1
        return float(last["nav_close"]), json.loads(last["weights"]), json.loads(last["closes"]), int(last["day"]) + DAY_MS

    def monitor(self) -> Optional[dict]:
        """Hourly mark of the paper book on 1h closes; applies the intraday caps. Returns the mark (or None)."""
        if self.risk is None or self.marks_fn is None:
            return None
        now = self.now_ms()
        bar = now // HOUR_MS * HOUR_MS - HOUR_MS  # newest closed 1h bar (open time)
        mark_ts = bar + HOUR_MS
        if int(self.ledger.risk_get("last_mark_bar", 0) or 0) >= bar:
            return None
        last = self.ledger.last_run()
        # Only the UTC day right after the last rebalance is marked intraday; if the daily run is
        # behind, its catch-up books those days at the daily closes.
        if last is None or not (int(last["day"]) + DAY_MS < mark_ts <= int(last["day"]) + 2 * DAY_MS):
            return None
        day_key = int(last["day"]) + DAY_MS  # the UTC day being marked
        nav_ref, w_ref, px_ref, f_from = self._ref_state(last)
        coins = [c for c, w in w_ref.items() if w]
        if not coins:  # flat book (e.g. locked): nothing to mark or reduce
            mark = {"ts": mark_ts, "nav": nav_ref, "dayRet": nav_ref / float(last["nav_close"]) - 1.0,
                    "drawdown": min(0.0, nav_ref / self.ledger.peak_nav(self.start_nav) - 1.0), "dataBad": False, "reason": "", "weights": {}}
            self.ledger.commit_risk([], {"last_mark_bar": bar, "last_mark": mark}, expect_day=int(last["day"]))
            return mark
        m = self.marks_fn(coins, bar) or {}
        if m.get("wait"):
            return None  # the bar's final close is not in the store yet; retry next tick
        prices = m.get("prices") or {}
        stale = sorted(c for c in coins if c not in prices or int(prices[c][0]) < bar - self.risk.stale_bars * HOUR_MS)
        data_bad_now = bool(stale) or m.get("ok") is False
        events: list[dict] = []
        state: dict = {"last_mark_bar": bar}
        was_bad = self.ledger.risk_get("data_bad")
        reason = ", ".join(x for x in ([f"stale 1h: {','.join(stale)}"] if stale else []) + ([str(m.get("reason") or "exchange health failed")] if m.get("ok") is False else []))
        if data_bad_now and not was_bad:
            events.append({"kind": "data_breaker", "action": "reduce_only", "detail": {"reason": reason}})
            state["data_bad"] = {"since": mark_ts, "reason": reason}
        elif data_bad_now:
            state["data_bad"] = {**was_bad, "reason": reason}
        elif was_bad:
            events.append({"kind": "data_breaker", "action": "cleared", "detail": {"since": was_bad.get("since")}})
            state["data_bad"] = None
        px = {c: (float(prices[c][1]) if c in prices else float(px_ref.get(c) or 0.0)) for c in coins}
        fund = {c: sum(x for _, x in self.funding_fn(c, f_from, mark_ts + 1)) if self.funding_fn else 0.0 for c in coins}
        pnl = sum(w_ref[c] * ((px[c] / float(px_ref[c]) - 1.0) if px_ref.get(c) else 0.0) - w_ref[c] * fund[c] for c in coins)
        g = 1.0 + pnl
        drifted = [w_ref[c] * (px[c] / float(px_ref[c]) if px_ref.get(c) else 1.0) / g if g > 0 else 0.0 for c in coins]
        nav_mark = nav_ref * g
        day_ret = nav_mark / float(last["nav_close"]) - 1.0
        peak = self.ledger.peak_nav(self.start_nav)
        dd = min(0.0, nav_mark / peak - 1.0)  # above the recorded peak = no drawdown
        flags = dict(self.ledger.risk_get(f"day:{day_key}", {}) or {})
        seen = dict(self.ledger.risk_get("funding_seen", {}) or {})
        f8_new = {}
        for c in coins:
            x = self._f8(c, mark_ts)
            if x and x[0] > int(seen.get(c, 0)):
                f8_new[c] = x[1]
                seen[c] = x[0]
        state["funding_seen"] = seen
        last_t = json.loads(last["risk_targets"] or last["targets"])
        new_w, ev, up = risk.intraday_actions(
            coins, drifted, lim=self.risk, day_ret=day_ret, dd=dd, flags=flags,
            dd_halved=self.ledger.risk_get("dd_halved_peak") == peak, f8_new=f8_new,
            last_targets=[abs(float(last_t.get(c, 0.0) or 0.0)) for c in coins])
        if self._locked(mark_ts) and any(new_w):
            new_w = [0.0] * len(coins)  # a lock keeps the book flat
        events += ev
        for k in ("stop_new", "halve", "flat"):
            if up.get(k):
                flags[k] = True
        if flags:
            state[f"day:{day_key}"] = flags
        if up.get("dd_halved"):
            state["dd_halved_peak"] = peak
        if up.get("lock") == "review":
            state["lock"] = {"kind": "review", "since": mark_ts, "reason": "drawdown <= %.0f%%" % (self.risk.dd_flat * 100)}
        elif up.get("lock") == "24h":
            state["lock"] = {"kind": "24h", "since": mark_ts, "until": mark_ts + int(self.risk.day_lock_hours * HOUR_MS),
                             "reason": "day loss <= %.0f%%" % (self.risk.day_flat * 100)}
        mark = {"ts": mark_ts, "nav": nav_mark, "dayRet": day_ret, "drawdown": dd, "peak": peak, "dataBad": data_bad_now,
                "reason": reason, "weights": {c: w for c, w in zip(coins, drifted)}}
        state["last_mark"] = mark
        adjustment, fills = None, []
        if any(abs(a - b) > 1e-12 for a, b in zip(new_w, drifted)):
            cost = sum(abs(a - b) * self.cost.per_side(c) for c, a, b in zip(coins, new_w, drifted))
            nav_after = nav_mark * (1.0 - cost)
            for c, a, b in zip(coins, new_w, drifted):
                if abs(a - b) <= 1e-12:
                    continue
                notional = abs(a - b) * nav_mark
                side = "buy" if a > b else "sell"
                slip = self.cost.slippage.get(c, self.cost.slippage_default) * self.cost.mult
                fills.append({"ts": mark_ts, "coin": c, "side": side, "w_from": b, "w_to": a, "notional": notional,
                              "qty": notional / px[c] if px[c] else 0.0, "price": px[c],
                              "fill_price": px[c] * (1 + slip if side == "buy" else 1 - slip),
                              "fee": notional * self.cost.taker * self.cost.mult, "slippage": notional * slip})
            all_px = {**{k: v for k, v in px_ref.items()}, **px}
            adjustment = {"ts": mark_ts, "day": int(last["day"]), "kinds": ",".join(sorted({e["kind"] for e in ev})) or "lock",
                          "nav_ref": nav_ref, "nav_mark": nav_mark, "nav_after": nav_after,
                          "pnl_usd": nav_mark - nav_ref, "funding_usd": nav_ref * sum(w_ref[c] * fund[c] for c in coins),
                          "cost_usd": nav_mark * cost, "turnover": sum(abs(a - b) for a, b in zip(new_w, drifted)),
                          "weights_before": json.dumps(dict(zip(coins, drifted))), "weights_after": json.dumps({c: a for c, a in zip(coins, new_w) if a}),
                          "prices": json.dumps(all_px)}
            mark["navAfter"] = nav_after
        for e in events:
            e.update(ts=mark_ts, day=int(last["day"]), at="intraday")
        if not self.ledger.commit_risk(events, state, adjustment=adjustment, fills=fills, expect_day=int(last["day"])):
            return None
        for e in events:
            log.warning("RISK %s %s intraday: value=%s threshold=%s %s", e["kind"], e["action"], e.get("value"), e.get("threshold"), e.get("detail"))
        return mark

    def clear_lock(self, note: str) -> bool:
        """Manual review done: lift a 'review' (or 24h) lock. Logged as a risk event."""
        lk = self.ledger.risk_get("lock")
        if not lk:
            return False
        ts = self.now_ms()
        self.ledger.commit_risk([{"ts": ts, "kind": "lock_cleared", "action": "manual", "detail": {"lock": lk, "note": note}, "at": "manual"}],
                                {"lock": None, "dd_halved_peak": None})
        return True

    def risk_summary(self) -> dict[str, Any]:
        if self.risk is None:
            return {"enabled": False}
        now = self.now_ms()
        lk = self.ledger.risk_get("lock")
        lr = self.ledger.last_run()
        today = int(lr["day"]) + DAY_MS if lr else now // DAY_MS * DAY_MS
        ev = []
        for x in self.ledger.risk_events(50):
            ev.append({"ts": int(x["ts"]), "day": ms_day(int(x["day"])) if x["day"] is not None else None, "kind": x["kind"],
                       "label": risk.KIND_LABEL.get(x["kind"], x["kind"]), "action": x["action"], "value": x["value"],
                       "threshold": x["threshold"], "detail": json.loads(x["detail"] or "{}"), "at": x["at"]})
        adj = [{"ts": int(a["ts"]), "kinds": a["kinds"], "navMark": a["nav_mark"], "navAfter": a["nav_after"], "cost": a["cost_usd"],
                "turnover": a["turnover"], "weightsBefore": json.loads(a["weights_before"]), "weightsAfter": json.loads(a["weights_after"])}
               for a in self.ledger.adjustments(20)]
        return {
            "enabled": True,
            "limits": self.risk.describe(),
            "locked": self._locked(now), "lock": lk,
            "dataBad": self.ledger.risk_get("data_bad"),
            "todayFlags": self.ledger.risk_get(f"day:{today}", {}) or {},
            "lastMark": self.ledger.risk_get("last_mark"),
            "events": ev, "eventCount": self._risk_count(), "adjustments": adj,
            "backtestNote": ("19 币留出期（2025-01-09 起）逐日/逐小时复核：单币最大 7%、总敞口最大 0.42、"
                             "盘中最差 −2.7%、最大回撤 −8.1%、持仓多头未遇到 > 0.1%/8h 的资金费，任何熔断都不会触发；"
                             "31/31 验收用回测引擎，不受这些上限影响。训练期（2021 牛市）资金费熔断会触发，年化 15.19% → 13.58%。"),
        }

    def _risk_count(self) -> int:
        with self.ledger._lock:
            return int(self.ledger._db.execute("SELECT COUNT(*) FROM risk_events").fetchone()[0])

    # ---- read side -----------------------------------------------------------------
    def status(self) -> dict[str, Any]:
        now = self.now_ms()
        last = self.ledger.last_run()
        if last:
            since = int(last["day"]) + DAY_MS  # the close the last rebalance traded at
        else:
            since = int(self.ledger.meta("created_at") or now)
        hours = max(0.0, (now - since) / 3_600_000)
        stalled = hours > STALL_HOURS
        return {
            "active": True,
            "strategy": self.strategy.name,
            "lastDay": ms_day(int(last["day"])) if last else None,
            "lastRunAt": int(last["ran_at"]) if last else None,
            "nextDueDay": ms_day((int(last["day"]) if last else self.due_day() - DAY_MS) + DAY_MS),
            "hoursSinceRebalance": round(hours, 2),
            "stallHours": STALL_HOURS,
            "stalled": stalled,
            "idleMin": int(hours * 60),
            "reason": ("no rebalance for %.1fh (> %gh)" % (hours, STALL_HOURS)) if stalled else "",
            "waiting": self.waiting,
            "lastError": self.last_error,
            "lastTickAt": self.last_tick_ms or None,
            "days": self._count(),
        }

    def _count(self) -> int:
        with self.ledger._lock:
            return int(self.ledger._db.execute("SELECT COUNT(*) FROM runs").fetchone()[0])

    def go_no_go(self, runs: Optional[list] = None) -> dict[str, Any]:
        runs = self.ledger.runs() if runs is None else runs
        n = len(runs)
        if self._go_cache and self._go_cache[0] == n:
            return self._go_cache[1]
        base = {"standard": "daily_returns", "days": n, "minDays": GO_MIN_DAYS, "tbill": stats.TBILL}
        if n < GO_MIN_DAYS:
            out = {**base, "verdict": "pending", "lamp": "gray",
                   "message": f"数据积累中，未证明优势（日收益 {n}/{GO_MIN_DAYS} 天）"}
        else:
            days = [int(x["day"]) for x in runs]
            rets = [float(x["ret"]) for x in runs]
            s = stats.summarize(days, rets, label="paper", rng=stats.StdRng(7))
            ci_ok = s["ci_lo"] > 0
            tb_ok = s["excess_ci"][0] > 0
            go = ci_ok and tb_ok
            why = [] if go else (["日收益年化 bootstrap CI 下限 ≤ 0"] if not ci_ok else []) + (["未跑赢国债（超额 CI 下限 ≤ 0）"] if not tb_ok else [])
            out = {**base, "verdict": "go" if go else "no-go", "lamp": "green" if go else "red",
                   "message": "通过：日收益 CI 下限 > 0 且跑赢国债" if go else "未通过：" + "；".join(why),
                   "stats": {k: (None if isinstance(s.get(k), float) and s[k] != s[k] else s.get(k))
                             for k in ("ann_mean", "cagr", "ci_lo", "ci_hi", "sharpe", "mdd", "excess_vs_tbill")}}
        self._go_cache = (n, out)
        return out

    def summary(self) -> dict[str, Any]:
        runs = self.ledger.runs()
        start_nav = float(self.ledger.meta("start_nav") or self.start_nav)
        curve, btc0 = [], None
        for j, x in enumerate(runs):
            closes = json.loads(x["closes"])
            btc = closes.get("BTC")
            if btc0 is None and btc:
                btc0 = btc
            curve.append({
                "day": ms_day(int(x["day"])), "ts": int(x["day"]), "nav": float(x["nav_close"]),
                "strategy": float(x["nav_close"]) / start_nav, "ret": float(x["ret"]),
                "cost": float(x["cost"]), "funding": float(x["funding"]), "gross": float(x["gross_after"]),
                "btc": (btc / btc0) if (btc and btc0) else None,
                "tbill": (1 + stats.TBILL) ** (j / 365), "catchup": bool(x["catchup"]),
            })
        positions = []
        if runs:
            last = runs[-1]
            nav = float(last["nav_close"])
            closes = json.loads(last["closes"])
            for c, w in sorted(json.loads(last["weights"]).items(), key=lambda kv: -abs(kv[1])):
                px = closes.get(c)
                positions.append({"coin": c, "weight": w, "notional": w * nav, "price": px,
                                  "qty": (w * nav / px) if px else None,
                                  "target": json.loads(last["targets"]).get(c)})
        bench = {}
        if curve:
            bench = {"strategy": curve[-1]["strategy"] - 1, "btc": (curve[-1]["btc"] - 1) if curve[-1]["btc"] else None,
                     "tbill": curve[-1]["tbill"] - 1}
        try:
            uni = dict(self.universe_fn() or {})
        except Exception as exc:
            uni = {"error": f"{type(exc).__name__}: {exc}"[:200]}
        current = json.loads(runs[-1]["universe"]) if runs and runs[-1]["universe"] else (list(json.loads(runs[-1]["targets"])) if runs else [])
        uni["current"] = current
        uni["changes"] = [{"day": ms_day(int(x["day"])), "coins": json.loads(x["coins"]), "prev": json.loads(x["prev"]),
                           "recordedAt": int(x["recorded_at"]), "note": x["note"]} for x in self.ledger.universe_changes()]
        uni["survivorship"] = ("研究币池是“今天仍存活”的 19 个大币，已退市或崩盘的币（如 LUNA、FTT）不在里面，"
                               "回测结果有幸存者偏差，偏乐观。")
        return {
            "strategy": self.strategy.describe(),
            "universe": uni,
            "ledger": "mainstream_strategy.sqlite",
            "mode": "paper",
            "live": {"enabled": False, "reason": "LIVE_API_LOCKED", "message": "实盘未开启"},
            "startNav": start_nav,
            "nav": curve[-1]["nav"] if curve else start_nav,
            "cost": {"taker": self.cost.taker, "slippage": self.cost.slippage, "slippageDefault": self.cost.slippage_default,
                     "band": BAND, "funding": "store funding rates, longs pay positive"},
            "curve": curve,
            "positions": positions,
            "fills": [dict(r) for r in self.ledger.fills(30)],
            "totals": bench,
            "goNoGo": self.go_no_go(runs),
            "status": self.status(),
            "risk": self.risk_summary(),
            "execShadow": _exec_shadow_summary(),
        }


def _exec_shadow_summary() -> Optional[dict]:
    try:
        from app.paper.exec_shadow import peek_summary

        return peek_summary()
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"[:200]}


# ---- production wiring (market store) ---------------------------------------------------
def _configured_universe(svc) -> list[str]:
    return list(svc.cfg.strategy_symbols or svc.cfg.symbols)


def _available(svc, ex: str) -> list[str]:
    """Universe coins the store has daily candles for (a coin the venue does not list is left out)."""
    return [c for c in _configured_universe(svc) if svc.store.candle_bounds(ex, c, "1d")[2]]


def _store_panel(day: int) -> Optional[Panel]:
    from app.backtest.panel import load_store
    from app.marketdata.mainstream import get_service

    svc = get_service()
    ex = svc.exchange_for_read()
    if not ex:
        return None
    coins = _available(svc, ex)
    p = load_store(svc.store, ex, coins, end=ms_day(day))
    return p.subset([c for c in coins if c in p.coins])


def _store_universe() -> dict:
    from app.marketdata.mainstream import get_service

    svc = get_service()
    ex = svc.exchange_for_read()
    configured = _configured_universe(svc)
    cov, unavailable = {}, {}
    for c in configured:
        first, last, n = svc.store.candle_bounds(ex, c, "1d") if ex else (None, None, 0)
        f_first, f_last, fn = svc.store.funding_bounds(ex, c) if ex else (None, None, 0)
        if not n:
            unavailable[c] = f"{ex or '?'} 没有 {c}/USDT 日线（未上架或拉取失败）"
            continue
        cov[c] = {"firstDay": ms_day(int(first)), "lastDay": ms_day(int(last)), "days": int(n),
                  "fundingFrom": ms_day(int(f_first)) if f_first else None, "fundingRows": int(fn)}
    return {"configured": configured, "exchange": ex, "coverage": cov, "unavailable": unavailable}


def _store_ready(day: int) -> tuple[bool, str]:
    from app.marketdata.mainstream import get_service

    svc = get_service()
    ex = svc.exchange_for_read()
    if not ex:
        return False, "no exchange data yet"
    missing = []
    for c in _available(svc, ex):
        _, last, _ = svc.store.candle_bounds(ex, c, "1d")
        # The store re-fetches its newest bar each pass, so a bar for D+1 means D is final.
        if last is None or int(last) < day + DAY_MS:
            missing.append(f"{c}:1d")
        _, flast, _ = svc.store.funding_bounds(ex, c)
        if flast is None or int(flast) < day + FUNDING_READY_MS:
            missing.append(f"{c}:funding")
    return (not missing), ",".join(missing)


def _store_marks(coins: list[str], bar: int) -> dict:
    """Newest closed 1h close per coin (open time <= bar) and the exchange health for the data breaker."""
    from app.marketdata.mainstream import get_service

    svc = get_service()
    ex = svc.exchange_for_read()
    if not ex:
        return {"prices": {}, "ok": False, "reason": "no exchange"}
    now = int(time.time() * 1000)
    last = svc.last_refresh_ms
    # The store re-fetches the forming bar; wait for a refresh after this bar closed (up to 15 min,
    # after which the mark proceeds and the staleness check decides).
    if (last is None or last < bar + HOUR_MS + 30_000) and now < bar + HOUR_MS + 15 * 60_000:
        return {"wait": True}
    prices = {}
    for c in coins:
        rows = svc.store.candles(ex, c, "1h", until=bar, limit=1)
        if rows:
            prices[c] = (int(rows[-1]["ts"]), float(rows[-1]["close"]))
    ok, reason = True, ""
    since = last if last is not None else _PROC_START_MS  # first refresh of this process may still be backfilling
    if now - since > 3 * max(60, svc.cfg.refresh_sec) * 1000 + 600_000:
        mins = f"{(now - since) / 60_000:.0f} min" + ("" if last is not None else " since start")
        ok, reason = False, f"no successful market refresh ({mins}): {svc.last_error or ''}"[:200]
    return {"prices": prices, "ok": ok, "reason": reason}


def _store_funding(coin: str, lo: int, hi: int) -> list[tuple[int, float]]:
    from app.marketdata.mainstream import get_service

    svc = get_service()
    ex = svc.exchange_for_read()
    if not ex:
        return []
    return [(int(x["ts"]), float(x["rate"])) for x in svc.store.funding(ex, coin, since=lo, limit=100_000) if int(x["ts"]) < hi]


def _exec_shadow_hook():
    if os.getenv("AUU_EXEC_SHADOW", "on").strip().lower() in {"0", "false", "off", "no"}:
        return None
    from app.paper.exec_shadow import on_commit

    return on_commit


def _store_exchange() -> Optional[str]:
    from app.marketdata.mainstream import get_service

    return get_service().exchange_for_read()


_runner: Optional[StrategyRunner] = None
_rlock = threading.Lock()
_PROC_START_MS = int(time.time() * 1000)


def peek_runner() -> Optional[StrategyRunner]:
    """The runner if it exists or its ledger file does (read paths never create a ledger)."""
    if _runner is not None:
        return _runner
    if (data_dir() / "mainstream_strategy.sqlite").exists():
        return get_runner()
    return None


def not_started_status() -> dict[str, Any]:
    """Health before the first tick created the ledger; stalls if that never happens."""
    hours = (int(time.time() * 1000) - _PROC_START_MS) / 3_600_000
    stalled = hours > STALL_HOURS
    return {"active": True, "strategy": TrendTSMOM().name, "lastDay": None, "days": 0, "stalled": stalled,
            "hoursSinceRebalance": round(hours, 2), "stallHours": STALL_HOURS, "idleMin": int(hours * 60),
            "reason": "runner never started" if stalled else "", "waiting": "runner starting"}


def get_runner() -> StrategyRunner:
    global _runner
    with _rlock:
        if _runner is None:
            _runner = StrategyRunner(StrategyLedger(), panel_fn=_store_panel, ready_fn=_store_ready,
                                     exchange_fn=_store_exchange, universe_fn=_store_universe,
                                     risk_limits=RiskLimits() if risk.risk_enabled() else None,
                                     marks_fn=_store_marks, funding_fn=_store_funding, on_commit=_exec_shadow_hook())
        return _runner


def reset_runner(runner: Optional[StrategyRunner] = None) -> None:
    global _runner
    with _rlock:
        _runner = runner


async def run_loop(interval_sec: int = 60) -> None:
    """Background task: tick every minute (cheap when nothing is due)."""
    import asyncio

    await asyncio.sleep(5)
    while True:
        try:
            await asyncio.to_thread(get_runner().tick)
        except Exception:  # never let the loop die
            log.exception("strategy loop")
        await asyncio.sleep(interval_sec)


def main(argv: Optional[list[str]] = None) -> int:
    """``python -m app.paper.strategy_runner clear-lock "<review note>"``: lift the risk lock after a manual review."""
    import sys

    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) >= 2 and args[0] == "clear-lock":
        r = StrategyRunner(StrategyLedger(), panel_fn=lambda d: None, ready_fn=lambda d: (False, ""), risk_limits=RiskLimits())
        done = r.clear_lock(" ".join(args[1:]))
        print("lock cleared" if done else "no lock")
        return 0
    print('usage: python -m app.paper.strategy_runner clear-lock "<review note>"')
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
