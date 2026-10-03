"""Shadow record of the S3 hypothesis: long after an extreme negative funding rate (no capital).

Frozen rule (leverage report §2 S3 / §3.3; code: research script leverage/hourly.py ``s3_funding`` + ``trade``, not in this repo),
long-only branch with the director's parameters threshold 0.1%/8h and hold 72h:

- at every funding settlement, f8 = rate * 8 / interval_hours (interval from the spacing to the
  previous settlement, default 8h);
- if f8 <= -0.001: enter long at the close of the 1h perp bar opening at floor(settlement hour),
  exit at the close of the bar opening 72h later;
- one trade per coin at a time: a signal whose hour <= the open trade's exit bar is skipped;
- net = price return - funding settled in (entry bar, exit bar] (longs pay positive)
  - 2 * (taker 0.05% + per-coin slippage).

Only settlements after the rule was registered count (out of sample). Hypothetical trades live in
``shadow_s3.sqlite``, separate from every paper ledger; nothing here sizes, books or sends an order.
At >= 100 closed trades the report's statistics run once (cluster by entry day, bootstrap 1000).
"""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

from app.backtest.costs import CostModel
from app.backtest.panel import DAY_MS, ms_day
from app.data_paths import data_dir

log = logging.getLogger("auu.shadow")

HOUR_MS = 3_600_000
RULE = {
    "id": "S3-long-negfund-abs0.001-hold72h",
    "signal": "funding settlement f8 = rate*8/interval_h <= -0.001 (-0.1%/8h)",
    "side": "long",
    "threshold_f8": -0.001,
    "hold_hours": 72,
    "entry": "close of the 1h perp bar opening at floor(settlement hour)",
    "exit": "close of the 1h perp bar opening hold_hours later",
    "overlap": "one trade per coin; signals at hours <= the open trade's exit bar are skipped",
    "costs": "2 x (taker 0.0005 + slippage: BTC/ETH 1bp, SOL/XRP/DOGE/BNB 2bp, others 3bp); funding settled in (entry, exit]",
    "source": "leverage report S3 posthoc long-only (hourly.py s3_funding/trade)",
}
RULE_HASH = hashlib.sha256(json.dumps(RULE, sort_keys=True).encode()).hexdigest()[:16]
EVAL_AT = 100
LABEL = "影子假设，非证据，需 ≥100 笔新交易再评估"
SETTLE_MS = 5 * 60_000  # ask for a bar's close 5 min after it closed
VOID_AFTER_MS = 48 * HOUR_MS  # a bar price still missing this long after it closed voids the trade

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS cursor (coin TEXT PRIMARY KEY, last_ts INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS trades (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  coin TEXT NOT NULL, signal_ts INTEGER NOT NULL, rate REAL NOT NULL, interval_h REAL NOT NULL, f8 REAL NOT NULL,
  entry_bar INTEGER NOT NULL, exit_bar INTEGER NOT NULL,
  status TEXT NOT NULL,            -- pending | open | closed | void
  entry_px REAL, exit_px REAL, gross REAL, funding REAL, cost REAL, net REAL,
  created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, note TEXT,
  UNIQUE (coin, signal_ts)
);
CREATE TABLE IF NOT EXISTS evaluations (
  milestone INTEGER PRIMARY KEY, ts INTEGER NOT NULL, n INTEGER NOT NULL, verdict TEXT NOT NULL, stats TEXT NOT NULL
);
"""

FundingFn = Callable[[str, int], list[tuple[int, float]]]  # coin, since_ms -> [(ts, rate)] ascending
PriceFn = Callable[[str, int], Optional[float]]  # coin, 1h bar open ts -> perp close (None = not available yet)


def interval_hours(prev_ts: Optional[int], ts: int) -> float:
    if prev_ts is None:
        return 8.0
    gap = (ts - prev_ts) / HOUR_MS
    return float(round(gap)) if 0.5 <= gap <= 8.5 else 8.0


def evaluate(trades: list[dict], seed: int = 5) -> dict:
    """Report statistics: mean net bp, entry-day cluster bootstrap CI (1000), win rate, ex-best-3."""
    import numpy as np

    net = np.array([t["net"] for t in trades], dtype=float)
    gross = np.array([t["gross"] for t in trades], dtype=float)
    by_day: dict[int, float] = {}
    for t in trades:
        d = int(t["entry_bar"]) // DAY_MS * DAY_MS
        by_day[d] = by_day.get(d, 0.0) + float(t["net"])
    dv = np.array(list(by_day.values()))
    nd = len(dv)
    per = len(trades) / nd
    rng = np.random.default_rng(seed)
    bs = [dv[rng.integers(0, nd, nd)].sum() / (per * nd) for _ in range(1000)]
    lo, hi = np.percentile(bs, [2.5, 97.5])
    ex3 = np.sort(net)[:-3] if len(net) > 3 else net[:0]
    first, last = min(int(t["entry_bar"]) for t in trades), max(int(t["entry_bar"]) for t in trades)
    yrs = max((last - first) / (365 * DAY_MS), 1 / 365)
    return {"n": len(trades), "days": nd, "mean_net_bps": float(net.mean() * 1e4), "ci_bps": [float(lo * 1e4), float(hi * 1e4)],
            "mean_gross_bps": float(gross.mean() * 1e4), "win": float((net > 0).mean()),
            "ex_best3_bps": float(ex3.mean() * 1e4) if len(ex3) else None, "trades_per_year": len(trades) / yrs}


class ShadowLedger:
    def __init__(self, path: Optional[Path | str] = None, *, now_ms: Callable[[], int] = lambda: int(time.time() * 1000)):
        self.path = Path(path) if path else data_dir() / "shadow_s3.sqlite"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None, timeout=30)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(_SCHEMA)
        self._lock = threading.RLock()
        now = str(now_ms())
        for k, v in (("registered_at", now), ("rule", json.dumps(RULE, sort_keys=True)), ("rule_hash", RULE_HASH)):
            self._db.execute("INSERT OR IGNORE INTO meta(key, value) VALUES (?, ?)", (k, v))

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def meta(self, key: str) -> Optional[str]:
        with self._lock:
            row = self._db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

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


class ShadowS3:
    def __init__(self, ledger: ShadowLedger, *, coins_fn: Callable[[], list[str]], funding_fn: FundingFn, price_fn: PriceFn,
                 cost: Optional[CostModel] = None, now_ms: Callable[[], int] = lambda: int(time.time() * 1000),
                 source_fn: Callable[[], Optional[str]] = lambda: None):
        self.ledger = ledger
        self.coins_fn, self.funding_fn, self.price_fn, self.source_fn = coins_fn, funding_fn, price_fn, source_fn
        self.cost = cost or CostModel()
        self.now_ms = now_ms
        self.registered_at = int(ledger.meta("registered_at") or now_ms())
        self.last_error = ""
        self._tick_lock = threading.Lock()

    def tick(self) -> dict:
        if not self._tick_lock.acquire(blocking=False):
            return {}
        try:
            out = {"signals": self._scan(), **self._advance()}
            out["evaluated"] = self._maybe_evaluate()
            self.last_error = ""
            return out
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"[:300]
            log.exception("shadow S3 tick failed")
            return {}
        finally:
            self._tick_lock.release()

    # ---- signals -------------------------------------------------------------------
    def _scan(self) -> int:
        n = 0
        hold = int(RULE["hold_hours"]) * HOUR_MS
        for c in self.coins_fn():
            cur = self.ledger.q("SELECT last_ts FROM cursor WHERE coin=?", (c,))
            last = int(cur[0]["last_ts"]) if cur else None
            since = (last if last is not None else self.registered_at) - 2 * DAY_MS  # previous settlement for the interval
            rows = sorted(self.funding_fn(c, since))
            prev = None
            new = []
            for ts, rate in rows:
                ts = int(ts)
                if ts >= self.registered_at and (last is None or ts > last):
                    iv = interval_hours(prev, ts)
                    new.append((ts, float(rate), iv, float(rate) * 8.0 / iv))
                prev = ts
            if not new:
                continue

            def write(db, c=c, new=new):
                k = 0
                busy = db.execute("SELECT MAX(exit_bar) FROM trades WHERE coin=? AND status!='void'", (c,)).fetchone()[0]
                busy = int(busy) if busy is not None else -1
                now = self.now_ms()
                for ts, rate, iv, f8 in new:
                    bar = ts // HOUR_MS * HOUR_MS
                    if f8 <= RULE["threshold_f8"] and bar > busy:
                        db.execute("INSERT OR IGNORE INTO trades(coin, signal_ts, rate, interval_h, f8, entry_bar, exit_bar, status, created_at, updated_at)"
                                   " VALUES (?,?,?,?,?,?,?, 'pending', ?, ?)", (c, ts, rate, iv, f8, bar, bar + hold, now, now))
                        busy = bar + hold
                        k += 1
                db.execute("INSERT OR REPLACE INTO cursor(coin, last_ts) VALUES (?, ?)", (c, new[-1][0]))
                return k

            got = self.ledger.tx(write)
            if got:
                log.warning("SHADOW S3 %d new hypothetical long(s) on %s (no capital)", got, c)
            n += got
        return n

    # ---- fills ---------------------------------------------------------------------
    def _advance(self) -> dict:
        now = self.now_ms()
        opened = closed = voided = 0
        for t in self.ledger.q("SELECT * FROM trades WHERE status IN ('pending','open') ORDER BY id"):
            if t["status"] == "pending" and now >= t["entry_bar"] + HOUR_MS + SETTLE_MS:
                px = self.price_fn(t["coin"], int(t["entry_bar"]))
                if px:
                    self._set(t["id"], status="open", entry_px=float(px))
                    opened += 1
                elif now > t["entry_bar"] + HOUR_MS + VOID_AFTER_MS:
                    self._set(t["id"], status="void", note="entry bar price unavailable")
                    voided += 1
                continue
            if t["status"] == "open" and now >= t["exit_bar"] + HOUR_MS + SETTLE_MS:
                px = self.price_fn(t["coin"], int(t["exit_bar"]))
                if not px:
                    if now > t["exit_bar"] + HOUR_MS + VOID_AFTER_MS:
                        self._set(t["id"], status="void", note="exit bar price unavailable")
                        voided += 1
                    continue
                c = t["coin"]
                fund = sum(r for ts, r in self.funding_fn(c, int(t["entry_bar"]))
                           if t["entry_bar"] < int(ts) // HOUR_MS * HOUR_MS <= t["exit_bar"])
                gross = float(px) / float(t["entry_px"]) - 1.0
                cost = 2.0 * (self.cost.taker + self.cost.slippage.get(c, self.cost.slippage_default))
                self._set(t["id"], status="closed", exit_px=float(px), gross=gross, funding=fund, cost=cost, net=gross - fund - cost)
                closed += 1
                log.warning("SHADOW S3 closed %s net=%.2f%% (hypothetical)", c, (gross - fund - cost) * 100)
        return {"opened": opened, "closed": closed, "voided": voided}

    def _set(self, tid: int, **kv) -> None:
        kv["updated_at"] = self.now_ms()
        cols = ", ".join(f"{k}=?" for k in kv)
        self.ledger.tx(lambda db: db.execute(f"UPDATE trades SET {cols} WHERE id=?", (*kv.values(), tid)))

    # ---- evaluation ----------------------------------------------------------------
    def closed(self) -> list[dict]:
        return [dict(r) for r in self.ledger.q("SELECT * FROM trades WHERE status='closed' ORDER BY entry_bar, id")]

    def _maybe_evaluate(self) -> Optional[dict]:
        tr = self.closed()
        if len(tr) < EVAL_AT or self.ledger.q("SELECT 1 FROM evaluations WHERE milestone=?", (EVAL_AT,)):
            return None
        st = evaluate(tr[:EVAL_AT])
        robust = st["ci_bps"][0] > 0 and (st["ex_best3_bps"] or 0) > 0
        verdict = "candidate" if robust else "fail"
        self.ledger.tx(lambda db: db.execute("INSERT OR IGNORE INTO evaluations(milestone, ts, n, verdict, stats) VALUES (?,?,?,?,?)",
                                             (EVAL_AT, self.now_ms(), EVAL_AT, verdict, json.dumps(st))))
        log.warning("SHADOW S3 evaluation at %d trades: %s %s", EVAL_AT, verdict, st)
        return {"milestone": EVAL_AT, "verdict": verdict, **st}

    # ---- read side -----------------------------------------------------------------
    def summary(self) -> dict[str, Any]:
        tr = [dict(r) for r in self.ledger.q("SELECT * FROM trades ORDER BY id DESC LIMIT 200")]
        closed = self.closed()
        counts = {r["status"]: int(r["n"]) for r in self.ledger.q("SELECT status, COUNT(*) AS n FROM trades GROUP BY status")}
        running = None
        if closed:
            net = [t["net"] for t in closed]
            running = {"n": len(closed), "mean_net_bps": sum(net) / len(net) * 1e4, "win": sum(1 for x in net if x > 0) / len(net),
                       "sum_net": sum(net)}
        evals = [{"milestone": int(e["milestone"]), "ts": int(e["ts"]), "n": int(e["n"]), "verdict": e["verdict"], **json.loads(e["stats"])}
                 for e in self.ledger.q("SELECT * FROM evaluations ORDER BY milestone")]
        stored = self.ledger.meta("rule_hash")
        return {
            "label": LABEL,
            "capital": 0,
            "ledger": "shadow_s3.sqlite (separate from the paper ledgers)",
            "rule": RULE, "ruleHash": RULE_HASH, "ruleFrozen": stored == RULE_HASH, "storedRuleHash": stored,
            "registeredAt": self.registered_at, "registeredDay": ms_day(self.registered_at // DAY_MS * DAY_MS),
            "source": self.source_fn(),
            "counts": {"pending": counts.get("pending", 0), "open": counts.get("open", 0), "closed": counts.get("closed", 0),
                       "void": counts.get("void", 0)},
            "evalAt": EVAL_AT, "progress": min(len(closed), EVAL_AT),
            "running": running,
            "runningNote": "未满 100 笔的累计数字只作记录，不是证据",
            "evaluations": evals,
            "trades": tr,
            "notes": [
                "参数按总监决定：阈值 0.1%/8h、持有 72h。报告 §3 第 3 条写的是 abs0.05%、持有 24h/72h，两者不一致，这里按总监的 0.1%/72h 写死。",
                "这条规则是看过留出数据后挑出的事后子集（报告：留出 22 笔/21 个月，CI 跨 0），只能当假设。",
                "按 0.1% 阈值，报告留出期约 1 笔/月，攒满 100 笔大约要 8 年；0.05% 阈值约 4 笔/月（约 2 年）。",
                "报告用 Binance 永续数据；这里用本机行情源（当前交易所）的公开资金费和永续 1h 收盘价。",
            ],
            "lastError": self.last_error,
        }


# ---- production wiring -----------------------------------------------------------------
_shadow: Optional[ShadowS3] = None
_slock = threading.Lock()


def _coins() -> list[str]:
    from app.backtest.panel import UNIVERSE_19
    from app.marketdata.mainstream import get_service

    cfg = get_service().cfg
    return list(cfg.strategy_symbols or UNIVERSE_19)


def _funding(coin: str, since: int) -> list[tuple[int, float]]:
    from app.marketdata.mainstream import get_service

    svc = get_service()
    ex = svc.exchange_for_read()
    if not ex:
        return []
    return [(int(x["ts"]), float(x["rate"])) for x in svc.store.funding(ex, coin, since=since, limit=100_000)]


_px_cache: dict[tuple[str, str, int], float] = {}


def _price(coin: str, bar: int) -> Optional[float]:
    """Close of the perp 1h bar opening at ``bar`` (public klines, fetched on demand)."""
    from app.marketdata.mainstream import get_service
    from app.marketdata.mainstream.fetcher import FetchError

    svc = get_service()
    ex = svc.exchange_for_read()
    if not ex:
        return None
    key = (ex, coin, bar)
    if key in _px_cache:
        return _px_cache[key]
    try:
        rows = svc._od_fetcher(ex).ohlcv(svc.cfg.perp(coin), "1h", bar, until=bar)
    except FetchError as exc:
        log.warning("shadow S3 price %s %s failed: %s", coin, bar, exc)
        return None
    for r in rows:
        if int(r[0]) == bar and r[4]:
            _px_cache[key] = float(r[4])
            return float(r[4])
    return None


def _source() -> Optional[str]:
    from app.marketdata.mainstream import get_service

    ex = get_service().exchange_for_read()
    return f"{ex} perp 1h + funding" if ex else None


def get_shadow() -> ShadowS3:
    global _shadow
    with _slock:
        if _shadow is None:
            _shadow = ShadowS3(ShadowLedger(), coins_fn=_coins, funding_fn=_funding, price_fn=_price, source_fn=_source)
        return _shadow


def reset_shadow(s: Optional[ShadowS3] = None) -> None:
    global _shadow
    with _slock:
        _shadow = s


def shadow_enabled() -> bool:
    import os

    return os.getenv("AUU_SHADOW_S3", "on").strip().lower() not in {"0", "false", "off", "no"}


async def run_loop(interval_sec: int = 300) -> None:
    import asyncio

    await asyncio.sleep(20)
    while True:
        try:
            await asyncio.to_thread(get_shadow().tick)
        except Exception:
            log.exception("shadow S3 loop")
        await asyncio.sleep(interval_sec)
