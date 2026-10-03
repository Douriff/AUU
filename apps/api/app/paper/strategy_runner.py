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

    def commit_day(self, run: dict, fills: list[dict], *, expect_prev: Optional[int], meta: dict[str, str],
                   universe_change: Optional[dict] = None) -> bool:
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
    ):
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
                return self._tick()
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

        r, f = [], []
        for c in coins:
            p1, p0 = close(c, i), close(c, i - 1)
            r.append(p1 / p0 - 1.0 if (p1 is not None and p0 not in (None, 0)) else 0.0)
            fs = panel.funding.get(c)
            f.append(float(fs[i]) if fs is not None else 0.0)
        targets = self.strategy.decide(panel.truncate(i + 1))
        tg = [float(targets.get(c, 0.0) or 0.0) for c in coins]
        w0 = [float(prev_w.get(c, 0.0)) for c in coins]
        cps = [self.cost.per_side(c) for c in coins]
        st = step(w0, r, f, tg, cps, band=BAND, min_trade=MIN_TRADE)
        nav_trade = nav_open * (1.0 + st.pnl)
        nav_close = nav_open * (1.0 + st.ret)
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
            "nav_open": nav_open, "pnl": st.pnl, "funding": st.funding, "cost": st.cost, "ret": st.ret,
            "nav_close": nav_close, "gross_before": st.gross_held, "gross_after": sum(abs(x) for x in st.weights),
            "turnover": st.turnover, "weights": json.dumps(weights), "targets": json.dumps({c: t for c, t in zip(coins, tg)}),
            "closes": json.dumps({c: close(c, i) for c in coins}),
            "universe": json.dumps(universe),
        }
        meta = {"strategy": self.strategy.name, "params": json.dumps(self.strategy.describe()),
                "start_day": str(day), "start_nav": str(self.start_nav)}
        ok = self.ledger.commit_day(run, fills, expect_prev=int(last["day"]) if last else None, meta=meta, universe_change=change)
        if ok:
            log.info("strategy %s rebalanced %s nav=%.2f ret=%.5f fills=%d%s", self.strategy.name, ms_day(day), nav_close, st.ret, len(fills), " (catch-up)" if catchup else "")
        return ok

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
        }


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
                                     exchange_fn=_store_exchange, universe_fn=_store_universe)
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
