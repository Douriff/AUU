"""H2 forward shadow record: trend (B058) + funding carry (Book A) + idle-cash yield. No capital.

Pre-registered 2026-10-04 (Beijing) in ``docs/hypotheses/H2_trend_carry_idle.{md,json}``.
Research: /workspace/research2 report §6 (post-hoc combination, hold-out 2025-01-09..2026-09-30
+5.24% [+3.12, +8.59], excess vs T-bill [-0.87, +4.60]: NOT a pass, so it is tested forward here).

Once per UTC day D, after D's daily bar closed and after the 08:00 (Beijing) paper rebalance, it
records what the frozen rule would have done at D's close:

- trend sleeve: B058 targets (``app.strategies.trend_b058``) traded with the backtest's
  ``engine.step`` (band 20%, min trade 1%, AUU ``CostModel``), idle capital earns ``idle.rate``;
- carry sleeve: research ``fa_backtest.sim_A`` "always" rule on BTC+ETH (long spot / short perp,
  3x, Binance VIP0 taker + slippage, liquidation check on the perp high), stepped on daily bars;
- the two sleeves are weighted by inverse trailing-60-day volatility, re-weighted at the close of
  the first UTC day of each month (0.17% per unit of capital moved).

The three legs (trend, carry, idle yield) are booked separately in ``shadow_h2.sqlite``. Nothing
here sizes, books, or sends an order, and it never opens any other ledger for writing.
The parameters are frozen: their SHA-256 must match the constant below, the registry JSON and the
ledger's stored hash, or the module refuses to run.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import sqlite3
import threading
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from app.backtest import stats
from app.backtest.costs import CostModel
from app.backtest.engine import step as engine_step
from app.backtest.panel import DAY_MS, Panel, day_ms, ms_day
from app.data_paths import data_dir
from app.strategies import trend_b058

log = logging.getLogger("auu.shadow_h2")

LEDGER_NAME = "shadow_h2.sqlite"
LABEL = "H2 影子盘：只记录、不下单、不投钱；未满 180 天的数字不是证据"

# ---- frozen parameters (do not edit: any change voids H2; register a new hypothesis instead) ----
PARAMS: dict = {
    "hypothesis": "H2",
    "name": "trend_carry_idle_v1",
    "trend": {
        "research_id": "B058",
        "universe": "BTC ETH SOL XRP DOGE BNB ADA AVAX LINK LTC TRX DOT BCH ETC XLM ATOM FIL UNI NEAR".split(),
        "signal": "agree3",
        "lookbacks": [20, 60, 120],
        "long_only": True,
        "per_coin_target_vol": 0.25,
        "per_coin_lev_cap": 2.0,
        "vol_window": 90,
        "elig_window": 90,
        "regime": {"coin": "BTC", "ma_days": 200, "bear_mult": 0.0},
        "portfolio_vol_target": 0.15,
        "cov_window": 60,
        "pvt_scale_cap": 10.0,
        "gross_cap": 2.0,
        "coin_cap": 0.10,
        "band": 0.2,
        "min_trade": 0.01,
        "cost": {"taker": 0.0005, "slippage": {"BTC": 0.0001, "ETH": 0.0001, "SOL": 0.0002, "XRP": 0.0002,
                                               "DOGE": 0.0002, "BNB": 0.0002}, "slippage_default": 0.0003, "mult": 1.0},
        "pnl_price": "perp close per the AUU market store (spot-close proxy, same as trend_tsmom_v1 paper); funding from the store, longs pay",
    },
    "carry": {
        "research_id": "funding_arb Book A",
        "coins": ["BTC", "ETH"],
        "weights": [0.5, 0.5],
        "mode": "always",
        "leverage": 3,
        "spot_fee": 0.0010,
        "perp_fee": 0.0005,
        "slippage": {"BTC": 0.0001, "ETH": 0.0001},
        "mmr": 0.005,
        "rebal_low": 0.5,
        "rebal_high": 2.0,
        "bars": "daily: spot close (store), perp close + high (exchange public 1d klines), funding = sum of the day's settlements (store)",
    },
    "idle": {"rate": 0.0399, "base": "trend sleeve capital not held in positions: max(0, 1 - gross held)",
             "source": "constant 3m T-bill 3.99% (AUU stats.TBILL, as research2 stage3 cash_yield); sUSDS ~3.6% noted as the realistic on-chain alternative"},
    "combine": {"mode": "inverse_vol_60d", "vol_window": 60, "rebalance": "close of the first UTC day of each month",
                "sleeve_cost": 0.0017, "fallback": [0.5, 0.5], "zero_vol": 1e-12,
                "zero_vol_rule": "a sleeve whose trailing 60d vol is <= zero_vol gets weight 1 (inverse-vol limit; e.g. a flat trend sleeve earning only idle yield); both zero -> fallback"},
    "benchmark": {"tbill": 0.0399},
    "gate": {"forward_days": 180, "statistic": "annualised mean daily return minus T-bill (stats.summarize excess_ci)",
             "ci": "95% moving-block bootstrap, block 20 days, 2000 resamples, StdRng(7)",
             "pass": "excess_ci_lo > 0 and no abort condition fired", "sample": "inception day (entry costs) + the next 180 UTC days"},
    "abort": {"max_drawdown": -0.05, "carry_liquidation": True, "lag_days_pause": 7, "lag_days_void": 30},
    "start": {"inception_day": "2026-10-04", "nav": 10000.0},
}


def params_hash(p: dict) -> str:
    return hashlib.sha256(json.dumps(p, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


FROZEN_SHA256 = "4795b3a1f15d2eb578a452bed53b3bf3c7609668aefef3f88abe870af43bc42b"
REGISTRY = Path(__file__).resolve().parents[4] / "docs" / "hypotheses" / "H2_trend_carry_idle.json"


class FrozenParamsError(RuntimeError):
    """The H2 parameters differ from the registered, hash-frozen set: refuse to run."""


def registry_path() -> Path:
    return Path(os.getenv("AUU_H2_REGISTRY") or REGISTRY)


def verify_frozen(params: Optional[dict] = None, *, registry: Optional[Path] = None, stored: Optional[str] = None) -> str:
    """Raise FrozenParamsError unless code params, effective cost model/T-bill, registry and ledger agree."""
    p = PARAMS if params is None else params
    h = params_hash(p)
    if h != FROZEN_SHA256:
        raise FrozenParamsError(f"params hash {h[:16]} != frozen {FROZEN_SHA256[:16]}")
    cm = asdict(CostModel())
    if cm != p["trend"]["cost"]:
        raise FrozenParamsError(f"effective CostModel {cm} != frozen trend cost {p['trend']['cost']}")
    if stats.TBILL != p["benchmark"]["tbill"]:
        raise FrozenParamsError(f"stats.TBILL {stats.TBILL} != frozen {p['benchmark']['tbill']}")
    path = registry or registry_path()
    try:
        reg = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception as exc:
        raise FrozenParamsError(f"registry {path} unreadable: {type(exc).__name__}: {exc}") from exc
    if reg.get("params_sha256") != FROZEN_SHA256 or params_hash(reg.get("params") or {}) != FROZEN_SHA256:
        raise FrozenParamsError(f"registry {Path(path).name}: stored {str(reg.get('params_sha256'))[:16]}, params hash "
                                f"{params_hash(reg.get('params') or {})[:16]}, frozen {FROZEN_SHA256[:16]}")
    if stored is not None and stored != FROZEN_SHA256:
        raise FrozenParamsError(f"ledger stored hash {stored[:16]} != frozen {FROZEN_SHA256[:16]}")
    return h


# ---- pure accounting -------------------------------------------------------------------
def _std(x: list[float]) -> float:
    n = len(x)
    m = sum(x) / n
    return math.sqrt(sum((v - m) ** 2 for v in x) / (n - 1))


def sleeve_targets(rt: list[float], rc: list[float], p: dict = PARAMS) -> tuple[float, float]:
    """Inverse-vol weights on the trailing window (research2 combo.combine 'rp')."""
    n = p["combine"]["vol_window"]
    fb = p["combine"]["fallback"]
    if len(rt) < n or len(rc) < n:
        return fb[0], fb[1]
    z = p["combine"]["zero_vol"]
    x, y = _std(rt[-n:]), _std(rc[-n:])
    x, y = (0.0 if x <= z else x), (0.0 if y <= z else y)
    if x == 0.0 and y == 0.0:
        return fb[0], fb[1]
    if x == 0.0 or y == 0.0:  # inverse-vol limit: the riskless sleeve takes everything (research2 pandas: ~0.99999)
        return (1.0, 0.0) if x == 0.0 else (0.0, 1.0)
    a, b = 1 / x, 1 / y
    return a / (a + b), b / (a + b)


def carry_entry(st: dict, sc: float, coin: str, p: dict = PARAMS) -> float:
    c = p["carry"]
    L = c["leverage"]
    fs, fp = c["spot_fee"] + c["slippage"][coin], c["perp_fee"] + c["slippage"][coin]
    tot = st["cash"] + st["q"] * sc + st["E"]
    N = tot / (1 + 1 / L)
    cost = N * (fs + fp)
    st["q"], st["E"], st["cash"] = N / sc, N / L, tot - N - N / L - cost
    st["entries"] = st.get("entries", 0) + 1
    return cost


def carry_step(st: dict, sc: float, pc: float, ph: float, f: float, coin: str, p: dict = PARAMS) -> dict:
    """One daily bar of fa_backtest.sim_A ('always', L=3): liquidation on the high, mark, funding, resize."""
    c = p["carry"]
    L, mmr = c["leverage"], c["mmr"]
    fs, fp = c["spot_fee"] + c["slippage"][coin], c["perp_fee"] + c["slippage"][coin]
    eq0 = st["cash"] + st["q"] * st["sc"] + st["E"]
    ev = {"liquidated": False, "rebalanced": False, "funding": 0.0, "cost": 0.0, "basis": 0.0}
    q, E, cash, pc0 = st["q"], st["E"], st["cash"], st["pc"]
    if q > 0:
        if E - q * (ph - pc0) <= mmr * q * ph:
            ev["liquidated"] = True
            E = 0.0
            cash += q * sc * (1 - fs)
            ev["cost"] += q * sc * fs
            q = 0.0
        else:
            ev["funding"] = q * pc * f
            E += q * (pc0 - pc) + q * pc * f
            im = q * pc / L
            if E < c["rebal_low"] * im or E > c["rebal_high"] * im:
                T = q * sc + E
                qn = T / (1 + 1 / L) / pc
                dq = q - qn
                cost = abs(dq) * sc * fs + abs(dq) * pc * fp  # research sim_A used signed dq (fee credit when sizing up); fees are never negative
                ev["cost"] += cost
                E = qn * pc / L
                cash += T - qn * sc - E - cost
                q = qn
                ev["rebalanced"] = True
                st["rebalances"] = st.get("rebalances", 0) + 1
    st.update(q=q, E=E, cash=cash, pc=pc, sc=sc)
    if q == 0 and c["mode"] == "always" and (cash + E) > 0:
        ev["cost"] += carry_entry(st, sc, coin, p)
    eq1 = st["cash"] + st["q"] * sc + st["E"]
    if ev["liquidated"]:
        st["liquidations"] = st.get("liquidations", 0) + 1
    ev["ret"] = eq1 / eq0 - 1.0 if eq0 > 0 else 0.0
    return ev


def new_carry_state(sc: float, pc: float) -> dict:
    return {"cash": 1.0, "q": 0.0, "E": 0.0, "sc": sc, "pc": pc, "entries": 0, "rebalances": 0, "liquidations": 0}


def trend_cps(coins: list[str]) -> list[float]:
    cm = CostModel()
    return [cm.per_side(c) for c in coins]


def perp_returns(panel: Panel, i: int) -> list[float]:
    out = []
    for c in panel.coins:
        px = panel.perp_close[c]
        a, b = (px[i - 1] if i > 0 else None), px[i]
        out.append(b / a - 1.0 if (a is not None and b is not None and a != 0) else 0.0)
    return out


def warmup_trend(panel: Panel, p: dict = PARAMS) -> list[tuple[int, float]]:
    """Trend sleeve (with idle yield) reconstructed over the whole panel: engine timing, causal."""
    W = trend_b058.targets(panel, p["trend"])
    coins = list(panel.coins)
    cps = trend_cps(coins)
    w = [0.0] * len(coins)
    out = []
    for i in range(len(panel)):
        r = perp_returns(panel, i)
        f = [panel.funding[c][i] for c in coins]
        st = engine_step(w, r, f, [W[c][i] for c in coins], cps, band=p["trend"]["band"], min_trade=p["trend"]["min_trade"],
                         cash_yield=p["idle"]["rate"])
        w = st.weights
        out.append((panel.days[i], st.ret))
    return out


def warmup_carry(spot: dict[str, dict[int, float]], perp: dict[str, dict[int, tuple[float, float]]],
                 funding: dict[str, dict[int, float]], days: list[int], p: dict = PARAMS) -> list[tuple[int, float]]:
    """Carry sleeve reconstructed on daily bars over ``days`` (first day = entry)."""
    coins = p["carry"]["coins"]
    sts, out = {}, []
    for k, d in enumerate(days):
        rs = []
        for c in coins:
            sc, (ph, pc) = spot[c][d], perp[c][d]
            if c not in sts:
                sts[c] = new_carry_state(sc, pc)
                e0 = carry_entry(sts[c], sc, c, p)
                rs.append(-e0)
                continue
            rs.append(carry_step(sts[c], sc, pc, ph, funding[c].get(d, 0.0), c, p)["ret"])
        out.append((d, sum(w * r for w, r in zip(p["carry"]["weights"], rs))))
    return out


# ---- ledger ----------------------------------------------------------------------------
_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS days (
  day INTEGER PRIMARY KEY, computed_at INTEGER NOT NULL, exchange TEXT, inception INTEGER NOT NULL DEFAULT 0,
  nav REAL NOT NULL, ret REAL NOT NULL, nav_trend REAL NOT NULL, nav_carry REAL NOT NULL,
  w_trend REAL NOT NULL, w_carry REAL NOT NULL,
  pnl_trend REAL NOT NULL, pnl_idle REAL NOT NULL, pnl_carry REAL NOT NULL, cost_sleeve REAL NOT NULL,
  rebalanced INTEGER NOT NULL, ret_trend_sleeve REAL NOT NULL, ret_carry_sleeve REAL NOT NULL,
  idle_ret REAL NOT NULL, gross_trend REAL NOT NULL, tbill_ret REAL NOT NULL, peak_nav REAL NOT NULL, drawdown REAL NOT NULL,
  state TEXT NOT NULL, targets TEXT NOT NULL, detail TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS warmup (sleeve TEXT NOT NULL, day INTEGER NOT NULL, ret REAL NOT NULL, PRIMARY KEY (sleeve, day));
CREATE TABLE IF NOT EXISTS perp_daily (exchange TEXT NOT NULL, coin TEXT NOT NULL, day INTEGER NOT NULL, high REAL NOT NULL,
  close REAL NOT NULL, PRIMARY KEY (exchange, coin, day));
CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, day INTEGER, kind TEXT NOT NULL, detail TEXT);
CREATE TABLE IF NOT EXISTS evaluations (milestone INTEGER PRIMARY KEY, ts INTEGER NOT NULL, verdict TEXT NOT NULL, stats TEXT NOT NULL);
"""


class H2Ledger:
    def __init__(self, path: Optional[Path | str] = None):
        self.path = path if path == ":memory:" else Path(path) if path else data_dir() / LEDGER_NAME
        if self.path != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None, timeout=30)
        self._db.row_factory = sqlite3.Row
        if self.path != ":memory:":
            self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(_SCHEMA)
        self._lock = threading.RLock()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def meta(self, key: str) -> Optional[str]:
        with self._lock:
            row = self._db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str, *, replace: bool = False) -> None:
        with self._lock:
            self._db.execute(f"INSERT OR {'REPLACE' if replace else 'IGNORE'} INTO meta(key, value) VALUES (?, ?)", (key, value))

    def q(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, args).fetchall()

    def tx(self, fn: Callable[[sqlite3.Connection], Any]) -> Any:
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                out = fn(self._db)
                self._db.execute("COMMIT")
                return out
            except Exception:
                self._db.execute("ROLLBACK")
                raise

    def rows(self) -> list[sqlite3.Row]:
        return self.q("SELECT * FROM days ORDER BY day")

    def last(self) -> Optional[sqlite3.Row]:
        r = self.q("SELECT * FROM days ORDER BY day DESC LIMIT 1")
        return r[0] if r else None


PanelFn = Callable[[int], Optional[Panel]]
PerpFn = Callable[[str, str, int, int], list[list]]  # exchange, coin, since_day, until_day -> klines [ts,o,h,l,c,v]


class ShadowH2:
    def __init__(self, ledger: H2Ledger, *, panel_fn: PanelFn, perp_fn: PerpFn, exchange_fn: Callable[[], Optional[str]],
                 ready_fn: Callable[[int], tuple[bool, str]] = lambda d: (True, ""), after_fn: Callable[[int], bool] = lambda d: True,
                 now_ms: Callable[[], int] = lambda: int(time.time() * 1000), registry: Optional[Path] = None,
                 inception_day: Optional[int] = None, params: Optional[dict] = None, settle_ms: int = 15 * 60_000,
                 max_catchup: int = 10):
        self.ledger, self.panel_fn, self.perp_fn, self.exchange_fn = ledger, panel_fn, perp_fn, exchange_fn
        self.ready_fn, self.after_fn, self.now_ms, self.registry = ready_fn, after_fn, now_ms, registry
        self.p = PARAMS if params is None else params
        self.inception = int(inception_day if inception_day is not None else day_ms(self.p["start"]["inception_day"]))
        self.settle_ms, self.max_catchup = settle_ms, max_catchup
        self.waiting = ""
        self.last_error = ""
        self.refused = ""
        self._tick_lock = threading.Lock()

    # ---- frozen check ----------------------------------------------------------------
    def check_frozen(self) -> str:
        h = verify_frozen(self.p, registry=self.registry, stored=self.ledger.meta("params_sha256"))
        self.ledger.set_meta("params_sha256", h)
        self.ledger.set_meta("registered_inception", ms_day(self.inception))
        return h

    # ---- schedule --------------------------------------------------------------------
    def due_day(self) -> int:
        return self.now_ms() // DAY_MS * DAY_MS - DAY_MS

    def tick(self) -> list[int]:
        if not self._tick_lock.acquire(blocking=False):
            return []
        try:
            try:
                self.check_frozen()
                self.refused = ""
            except FrozenParamsError as exc:
                self.refused = str(exc)[:300]
                self.last_error = f"FROZEN PARAMS MISMATCH: {exc}"[:300]
                log.error("H2 shadow refuses to run: %s", exc)
                return []
            out = self._tick()
            self.last_error = ""
            return out
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"[:300]
            log.exception("H2 shadow tick failed")
            return []
        finally:
            self._tick_lock.release()

    def _tick(self) -> list[int]:
        due = self.due_day()
        last = self.ledger.last()
        start = self.inception if last is None else int(last["day"]) + DAY_MS
        if due < start:
            self.waiting = f"next day {ms_day(start)} not closed yet"
            return []
        if self.now_ms() < due + DAY_MS + self.settle_ms:
            due -= DAY_MS  # newest bar closed < settle_ms ago: leave it for the next tick
            if due < start:
                self.waiting = f"{ms_day(start)} closed < {self.settle_ms // 60_000} min ago"
                return []
        days = list(range(start, due + 1, DAY_MS))
        if len(days) > self.max_catchup:
            self.waiting = f"behind {len(days)} days (> {self.max_catchup}); manual review"
            return []
        if not self.after_fn(due):
            self.waiting = f"waiting for the paper rebalance of {ms_day(due)}"
            return []
        ok, why = self.ready_fn(due)
        if not ok:
            self.waiting = f"waiting for {ms_day(due)} data: {why}"[:300]
            return []
        panel = self.panel_fn(due)
        if panel is None or not len(panel) or panel.days[-1] != due:
            self.waiting = f"panel for {ms_day(due)} not available"
            return []
        ex = self.exchange_fn() or "?"
        perp = self._perp(ex, min(days) - 160 * DAY_MS if last is None else min(days) - DAY_MS, due)
        written = []
        for d in days:
            if any(d not in perp[c] for c in self.p["carry"]["coins"]):
                self.waiting = f"perp 1d kline for {ms_day(d)} missing"
                break
            self._run_day(panel, panel.days.index(d), perp, ex)
            written.append(d)
        else:
            self.waiting = ""
        if written:
            self._maybe_evaluate()
        return written

    def _perp(self, ex: str, lo: int, hi: int) -> dict[str, dict[int, tuple[float, float]]]:
        out: dict[str, dict[int, tuple[float, float]]] = {}
        for c in self.p["carry"]["coins"]:
            have = {int(r["day"]): (float(r["high"]), float(r["close"]))
                    for r in self.ledger.q("SELECT day, high, close FROM perp_daily WHERE exchange=? AND coin=? AND day>=? AND day<=?",
                                           (ex, c, lo, hi))}
            if any(d not in have for d in range(lo, hi + 1, DAY_MS)):
                rows = self.perp_fn(ex, c, lo, hi) or []
                new = [(ex, c, int(r[0]) // DAY_MS * DAY_MS, float(r[2]), float(r[4])) for r in rows
                       if r and r[0] is not None and r[2] and r[4] and lo <= int(r[0]) <= hi and int(r[0]) + DAY_MS <= self.now_ms()]
                if new:
                    self.ledger.tx(lambda db, new=new: db.executemany("INSERT OR REPLACE INTO perp_daily VALUES (?,?,?,?,?)", new))
                have.update({d: (h, cl) for _, _, d, h, cl in new})
            out[c] = have
        return out

    # ---- one day ---------------------------------------------------------------------
    def _history(self, sleeve: str, upto: int, n: int) -> list[float]:
        col = "ret_trend_sleeve" if sleeve == "trend" else "ret_carry_sleeve"
        fw = {int(r["day"]): float(r[col]) for r in self.ledger.q(f"SELECT day, {col} FROM days WHERE inception=0 AND day<=?", (upto,))}
        wu = {int(r["day"]): float(r["ret"]) for r in self.ledger.q("SELECT day, ret FROM warmup WHERE sleeve=? AND day<=?", (sleeve, upto))}
        wu.update(fw)
        return [wu[d] for d in sorted(wu)][-n:]

    def _warmup(self, panel: Panel, i: int, perp) -> None:
        p = self.p
        sub = panel.truncate(i + 1)
        tr = warmup_trend(sub, p)[-p["combine"]["vol_window"]:]
        d0 = panel.days[i]
        cdays = [d for d in sub.days if d >= d0 - 150 * DAY_MS and all(d in perp[c] for c in p["carry"]["coins"])
                 and all(sub.spot_close[c][sub.days.index(d)] is not None for c in p["carry"]["coins"])]
        spot = {c: {d: sub.spot_close[c][sub.days.index(d)] for d in cdays} for c in p["carry"]["coins"]}
        fund = {c: {d: sub.funding[c][sub.days.index(d)] for d in cdays} for c in p["carry"]["coins"]}
        cr = warmup_carry(spot, perp, fund, cdays, p)[1:][-p["combine"]["vol_window"]:]
        rows = [("trend", d, r) for d, r in tr] + [("carry", d, r) for d, r in cr]
        self.ledger.tx(lambda db: db.executemany("INSERT OR REPLACE INTO warmup VALUES (?,?,?)", rows))

    def _run_day(self, panel: Panel, i: int, perp, ex: str) -> dict:
        p = self.p
        d = panel.days[i]
        coins = list(panel.coins)
        last = self.ledger.last()
        tg = trend_b058.decide(panel.truncate(i + 1), p["trend"])
        cps = trend_cps(coins)
        yr = p["idle"]["rate"]
        cc = p["carry"]["coins"]
        sc = {c: panel.spot_close[c][i] for c in cc}
        if any(v is None for v in sc.values()):
            raise RuntimeError(f"spot close missing for carry on {ms_day(d)}")
        detail: dict[str, Any] = {"coins": coins}
        if last is None:  # inception: allocate at D0's close, sleeves trade in from flat
            self._warmup(panel, i, perp)
            st = engine_step([0.0] * len(coins), [0.0] * len(coins), [0.0] * len(coins), [tg.get(c, 0.0) for c in coins], cps,
                             band=p["trend"]["band"], min_trade=p["trend"]["min_trade"], cash_yield=0.0)
            idle = 0.0
            r_t = st.ret
            cst = {}
            rcs = []
            for c in cc:
                cst[c] = new_carry_state(sc[c], perp[c][d][1])
                rcs.append(-carry_entry(cst[c], sc[c], c, p))
            r_c = sum(w * r for w, r in zip(p["carry"]["weights"], rcs))
            wt, wc = sleeve_targets(self._history("trend", d, 10**6), self._history("carry", d, 10**6), p)
            nav0 = float(p["start"]["nav"])
            capT0, capC0 = wt * nav0, wc * nav0
            cost_sleeve, rebalanced, peak_prev = 0.0, 1, nav0
            detail.update(trend_trades=len(st.trades), trend_cost=st.cost, carry_entry_cost=-r_c)
        else:
            s0 = json.loads(last["state"])
            w0 = [s0["trend_w"].get(c, 0.0) for c in coins]
            r = perp_returns(panel, i)
            f = [panel.funding[c][i] for c in coins]
            st = engine_step(w0, r, f, [tg.get(c, 0.0) for c in coins], cps, band=p["trend"]["band"],
                             min_trade=p["trend"]["min_trade"], cash_yield=yr)
            idle = max(0.0, 1.0 - st.gross_held) * yr / 365
            r_t = st.ret
            cst = s0["carry"]
            rcs, cev = [], {}
            for c in cc:
                ph, pc = perp[c][d]
                ev = carry_step(cst[c], sc[c], pc, ph, panel.funding[c][i], c, p)
                rcs.append(ev["ret"])
                cev[c] = ev
            r_c = sum(w * r for w, r in zip(p["carry"]["weights"], rcs))
            capT0, capC0 = float(last["nav_trend"]), float(last["nav_carry"])
            peak_prev = float(last["peak_nav"])
            detail.update(trend_trades=len(st.trades), trend_cost=st.cost, trend_funding=st.funding, carry=cev)
            cost_sleeve, rebalanced = 0.0, 0
        pnl_trend, pnl_idle, pnl_carry = capT0 * (r_t - idle), capT0 * idle, capC0 * r_c
        capT, capC = capT0 * (1 + r_t), capC0 * (1 + r_c)
        month = ms_day(d)[:7]
        if last is not None and month != json.loads(last["state"])["month"]:
            # month boundary: write today's sleeve returns first so the window ends at D (research timing)
            ht = self._history("trend", d - DAY_MS, 10**6) + [r_t]
            hc = self._history("carry", d - DAY_MS, 10**6) + [r_c]
            wt, wc = sleeve_targets(ht, hc, p)
            tot = capT + capC
            frac = (abs(wt - capT / tot) + abs(wc - capC / tot)) / 2 * p["combine"]["sleeve_cost"]
            cost_sleeve = tot * frac
            tot -= cost_sleeve
            capT, capC = wt * tot, wc * tot
            rebalanced = 1
        nav = capT + capC
        nav_prev = float(p["start"]["nav"]) if last is None else float(last["nav"])
        peak = max(peak_prev, nav)
        state = {"trend_w": {c: w for c, w in zip(coins, st.weights) if w}, "carry": cst, "month": month}
        row = dict(day=d, computed_at=self.now_ms(), exchange=ex, inception=1 if last is None else 0, nav=nav, ret=nav / nav_prev - 1,
                   nav_trend=capT, nav_carry=capC, w_trend=capT / nav, w_carry=capC / nav, pnl_trend=pnl_trend, pnl_idle=pnl_idle,
                   pnl_carry=pnl_carry, cost_sleeve=cost_sleeve, rebalanced=rebalanced, ret_trend_sleeve=r_t, ret_carry_sleeve=r_c,
                   idle_ret=idle, gross_trend=sum(abs(w) for w in st.weights), tbill_ret=0.0 if last is None else p["benchmark"]["tbill"] / 365,
                   peak_nav=peak, drawdown=nav / peak - 1, state=json.dumps(state, sort_keys=True),
                   targets=json.dumps({c: v for c, v in tg.items() if v}, sort_keys=True), detail=json.dumps(detail, sort_keys=True, default=str))
        cols = ",".join(row)

        def write(db):
            db.execute(f"INSERT INTO days({cols}) VALUES ({','.join('?' * len(row))})", tuple(row.values()))
            for kind, hit, info in (("abort:drawdown", row["drawdown"] <= p["abort"]["max_drawdown"], f"drawdown {row['drawdown']:.4f}"),
                                    ("abort:carry_liquidation", p["abort"]["carry_liquidation"] and any(
                                        (detail.get("carry") or {}).get(c, {}).get("liquidated") for c in cc), "carry leg liquidated")):
                if hit:
                    db.execute("INSERT INTO events(ts, day, kind, detail) VALUES (?,?,?,?)", (self.now_ms(), d, kind, info))
                    db.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('aborted', ?)", (f"{ms_day(d)} {kind}: {info}",))

        self.ledger.tx(write)
        log.warning("H2 SHADOW %s nav=%.2f ret=%+.4f%% (hypothetical, no orders)", ms_day(d), nav, row["ret"] * 100)
        return row

    # ---- evaluation ------------------------------------------------------------------
    def _maybe_evaluate(self) -> Optional[dict]:
        n = self.p["gate"]["forward_days"]
        rows = self.ledger.rows()
        if len(rows) < n + 1 or self.ledger.q("SELECT 1 FROM evaluations WHERE milestone=?", (n,)):
            return None
        sample = rows[: n + 1]
        s = stats.summarize([int(r["day"]) for r in sample], [float(r["ret"]) for r in sample], label="H2 forward",
                            tbill=self.p["benchmark"]["tbill"], rng=stats.StdRng(7))
        aborted = self.ledger.meta("aborted")
        verdict = "pass" if (s["excess_ci"][0] > 0 and not aborted) else "fail"
        self.ledger.tx(lambda db: db.execute("INSERT OR IGNORE INTO evaluations VALUES (?,?,?,?)",
                                             (n, self.now_ms(), verdict, json.dumps(s, default=str))))
        log.warning("H2 SHADOW evaluation at %d days: %s", n, verdict)
        return {"verdict": verdict, **s}

    # ---- read side -------------------------------------------------------------------
    def summary(self, *, rows: int = 200) -> dict[str, Any]:
        rs = self.ledger.rows()
        n_fw = sum(1 for r in rs if not r["inception"])
        last = rs[-1] if rs else None
        cum = None
        if last is not None:
            nav0 = float(self.p["start"]["nav"])
            tb = (1 + self.p["benchmark"]["tbill"] / 365) ** n_fw - 1
            cr = float(last["nav"]) / nav0 - 1
            cum = {"ret": cr, "tbill": tb, "excess": cr - tb, "pnlTrend": sum(float(r["pnl_trend"]) for r in rs),
                   "pnlCarry": sum(float(r["pnl_carry"]) for r in rs), "pnlIdle": sum(float(r["pnl_idle"]) for r in rs),
                   "costSleeve": sum(float(r["cost_sleeve"]) for r in rs), "maxDrawdown": min(float(r["drawdown"]) for r in rs)}
        interim = None
        if len(rs) >= 90:
            try:
                interim = stats.summarize([int(r["day"]) for r in rs], [float(r["ret"]) for r in rs], label="H2 interim (non-binding)",
                                          tbill=self.p["benchmark"]["tbill"], rng=stats.StdRng(7), n_boot=500)
            except Exception as exc:
                interim = {"error": f"{type(exc).__name__}: {exc}"[:200]}
        lag = None
        if last is not None:
            lag = int((self.due_day() - int(last["day"])) // DAY_MS)
        stored = self.ledger.meta("params_sha256")
        try:
            verify_frozen(self.p, registry=self.registry, stored=stored)
            frozen_ok = True
        except FrozenParamsError:
            frozen_ok = False
        return {
            "label": LABEL, "capital": 0, "ledger": f"{LEDGER_NAME} (separate from every paper ledger)",
            "hypothesis": "H2", "paramsSha256": FROZEN_SHA256, "storedParamsSha256": stored, "paramsFrozen": frozen_ok,
            "refused": self.refused, "registry": "docs/hypotheses/H2_trend_carry_idle.{md,json}",
            "inceptionDay": ms_day(self.inception), "gate": self.p["gate"], "abort": self.p["abort"],
            "aborted": self.ledger.meta("aborted"), "progress": min(n_fw, self.p["gate"]["forward_days"]),
            "forwardDays": n_fw, "lagDays": lag,
            "paused": bool(lag is not None and lag > self.p["abort"]["lag_days_pause"]),
            "voidByLag": bool(lag is not None and lag > self.p["abort"]["lag_days_void"]),
            "last": None if last is None else {k: last[k] for k in last.keys() if k not in ("state", "detail")} | {
                "dayStr": ms_day(int(last["day"])), "targets": json.loads(last["targets"]), "state": json.loads(last["state"])},
            "cumulative": cum, "interim": interim,
            "evaluations": [{"milestone": int(e["milestone"]), "ts": int(e["ts"]), "verdict": e["verdict"], **json.loads(e["stats"])}
                            for e in self.ledger.q("SELECT * FROM evaluations ORDER BY milestone")],
            "events": [dict(e) for e in self.ledger.q("SELECT * FROM events ORDER BY id DESC LIMIT 50")],
            "rows": [{k: r[k] for k in r.keys() if k not in ("state", "detail", "targets")} for r in rs[-rows:]],
            "waiting": self.waiting, "lastError": self.last_error,
        }

    def digest_line(self) -> str:
        s = self.summary(rows=1)
        if s["refused"]:
            return f"⚠ 参数哈希不一致，拒绝运行：{s['refused'][:120]}"
        last, cum = s["last"], s["cumulative"]
        if last is None:
            return f"已登记（参数 {FROZEN_SHA256[:12]}），首个记录日 {s['inceptionDay']} 收盘（北京时间次日 08:15 后）{('；' + s['waiting']) if s['waiting'] else ''}"
        out = (f"{last['dayStr']} 当日 {last['ret'] * 100:+.3f}% · 累计 {cum['ret'] * 100:+.3f}%（T-bill 同期 {cum['tbill'] * 100:+.3f}%，"
               f"超额 {cum['excess'] * 100:+.3f}%）· NAV {last['nav']:,.2f} · 腿：趋势 {cum['pnlTrend']:+.2f} / 套利 {cum['pnlCarry']:+.2f} / "
               f"闲置 {cum['pnlIdle']:+.2f} USDT（调仓成本 {cum['costSleeve']:.2f}）· 权重 趋势 {last['w_trend'] * 100:.1f}% / 套利 "
               f"{last['w_carry'] * 100:.1f}% · 进度 {s['progress']}/{self.p['gate']['forward_days']} 天")
        if s["aborted"]:
            out += f" · ⚠ 已触发中止条件：{s['aborted']}"
        if s["paused"]:
            out += f" · ⚠ 落后 {s['lagDays']} 天"
        return out


# ---- production wiring -----------------------------------------------------------------
def _panel(day: int) -> Optional[Panel]:
    from app.backtest.panel import load_store
    from app.marketdata.mainstream import get_service

    svc = get_service()
    ex = svc.exchange_for_read()
    if not ex:
        return None
    coins = [c for c in PARAMS["trend"]["universe"] if svc.store.candle_bounds(ex, c, "1d")[2]]
    p = load_store(svc.store, ex, coins, end=ms_day(day))
    return p.subset([c for c in coins if c in p.coins])


def _perp_klines(ex: str, coin: str, lo: int, hi: int) -> list[list]:
    from app.marketdata.mainstream import get_service
    from app.marketdata.mainstream.fetcher import FetchError

    svc = get_service()
    try:
        return svc._od_fetcher(ex).ohlcv(svc.cfg.perp(coin), "1d", lo, until=hi)
    except FetchError as exc:
        log.warning("H2 shadow perp %s %s failed: %s", ex, coin, exc)
        return []


def _exchange() -> Optional[str]:
    from app.marketdata.mainstream import get_service

    return get_service().exchange_for_read()


def _ready(day: int) -> tuple[bool, str]:
    from app.paper.strategy_runner import _store_ready

    return _store_ready(day)


def _after_paper(day: int) -> bool:
    """True once the paper runner recorded ``day`` (read-only), or 60 min after the close regardless."""
    if int(time.time() * 1000) >= day + DAY_MS + 60 * 60_000:
        return True
    try:
        from app.paper.strategy_runner import peek_runner

        r = peek_runner()
        lr = r.ledger.last_run() if r is not None else None
        return lr is not None and int(lr["day"]) >= day
    except Exception:
        return False


_shadow: Optional[ShadowH2] = None
_slock = threading.Lock()


def get_shadow() -> ShadowH2:
    global _shadow
    with _slock:
        if _shadow is None:
            _shadow = ShadowH2(H2Ledger(), panel_fn=_panel, perp_fn=_perp_klines, exchange_fn=_exchange, ready_fn=_ready,
                               after_fn=_after_paper)
        return _shadow


def peek_shadow() -> Optional[ShadowH2]:
    if _shadow is not None:
        return _shadow
    if (data_dir() / LEDGER_NAME).exists():
        return get_shadow()
    return None


def reset_shadow(s: Optional[ShadowH2] = None) -> None:
    global _shadow
    with _slock:
        _shadow = s


def shadow_enabled() -> bool:
    from app.role import is_standby

    if is_standby():  # AUU_ROLE=standby: the primary writes the H2 record
        return False
    return os.getenv("AUU_SHADOW_H2", "on").strip().lower() not in {"0", "false", "off", "no"}


async def run_loop(interval_sec: int = 300) -> None:
    import asyncio

    await asyncio.sleep(60)
    while True:
        try:
            await asyncio.to_thread(get_shadow().tick)
        except Exception:
            log.exception("H2 shadow loop")
        await asyncio.sleep(interval_sec)


def preview(now_ms: Optional[int] = None) -> dict:
    """Self-check: frozen hash + a full inception computation at the newest closed day, in memory (writes nothing)."""
    now = now_ms or int(time.time() * 1000)
    h = verify_frozen()
    due = now // DAY_MS * DAY_MS - DAY_MS
    led = H2Ledger(":memory:")
    s = ShadowH2(led, panel_fn=_panel, perp_fn=_perp_klines, exchange_fn=_exchange, ready_fn=_ready, now_ms=lambda: now,
                 inception_day=due, settle_ms=0)
    got = s.tick()
    out = {"paramsSha256": h, "previewDay": ms_day(due), "written": [ms_day(d) for d in got], "waiting": s.waiting,
           "lastError": s.last_error}
    if got:
        r = led.last()
        out.update(exchange=r["exchange"], w_trend=r["w_trend"], w_carry=r["w_carry"], nav=r["nav"], ret=r["ret"],
                   trend_targets=json.loads(r["targets"]), gross_trend=r["gross_trend"], detail=json.loads(r["detail"]),
                   warmup={k: len(led.q("SELECT 1 FROM warmup WHERE sleeve=?", (k,))) for k in ("trend", "carry")},
                   warmup_ann={k: (sum(float(x["ret"]) for x in led.q("SELECT ret FROM warmup WHERE sleeve=?", (k,))) /
                                   max(1, len(led.q("SELECT 1 FROM warmup WHERE sleeve=?", (k,)))) * 365) for k in ("trend", "carry")})
    led.close()
    return out


def main(argv: Optional[list[str]] = None) -> int:
    """python -m app.paper.shadow_h2 hash | preview | status"""
    import sys

    args = list(sys.argv[1:] if argv is None else argv)
    cmd = args[0] if args else "status"
    if cmd == "hash":
        print(json.dumps({"computed": params_hash(PARAMS), "frozen": FROZEN_SHA256, "verify": verify_frozen()}))
        return 0
    if cmd == "preview":
        print(json.dumps(preview(), indent=1, default=str, ensure_ascii=False))
        return 0
    if cmd == "status":
        s = peek_shadow()
        print(json.dumps(s.summary(rows=5) if s else {"ledger": "not created yet"}, indent=1, default=str, ensure_ascii=False))
        return 0
    print("usage: python -m app.paper.shadow_h2 hash | preview | status")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
