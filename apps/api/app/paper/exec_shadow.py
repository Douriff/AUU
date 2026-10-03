"""Execution-price shadow record (P0-2): what the paper fills would have cost against the real book.

After each rebalance the runner hands its fills to :func:`record`, which reads the public L2 order
book of the perp for every traded coin (no key) and stores, per fill:

- bid / ask / mid / spread, and the VWAP of walking the book for the fill's notional;
- shortfall vs mid (pure execution cost: half spread + depth) and vs the ledger close price
  (also includes the drift between the 00:00 UTC close and the snapshot);
- deviation = shortfall vs mid minus the assumed per-coin slippage (taker fee is the same either way).

Stored in ``exec_shadow.sqlite`` only. Nothing here is read back by the ledger or changes a fill,
so paper-vs-backtest equivalence is untouched. Catch-up days are recorded as skipped (the book
at replay time says nothing about that day's close).
"""
from __future__ import annotations

import json
import logging
import queue
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

from app.backtest.costs import CostModel
from app.backtest.panel import ms_day
from app.data_paths import data_dir

log = logging.getLogger("auu.exec_shadow")
BOOK_LEVELS = 100

_SCHEMA = """
CREATE TABLE IF NOT EXISTS quotes (
  day INTEGER NOT NULL, coin TEXT NOT NULL,
  status TEXT NOT NULL,            -- ok | partial (book shallower than the order) | skipped_catchup | error
  ts INTEGER NOT NULL,             -- snapshot time (ms)
  exchange TEXT, symbol TEXT, side TEXT NOT NULL, notional REAL NOT NULL,
  ledger_price REAL, assumed_slip REAL NOT NULL,
  bid REAL, ask REAL, mid REAL, spread_bp REAL, vwap REAL, filled_notional REAL, levels INTEGER,
  shortfall_mid_bp REAL, shortfall_close_bp REAL, deviation_bp REAL, error TEXT,
  PRIMARY KEY (day, coin)
);
"""


def walk_book(levels: list[list[float]], notional: float) -> tuple[Optional[float], float, int]:
    """VWAP of taking ``notional`` (quote currency) from price levels [[price, base_qty], ...]."""
    left, cost_q, base, used = float(notional), 0.0, 0.0, 0
    for lv in levels:
        px, qty = float(lv[0]), float(lv[1])
        if px <= 0 or qty <= 0:
            continue
        take = min(qty, left / px)
        base += take
        cost_q += take * px
        left -= take * px
        used += 1
        if left <= 1e-9:
            break
    return (cost_q / base if base else None), cost_q, used


def measure(book: dict, side: str, notional: float, ledger_price: Optional[float], assumed_slip: float) -> dict:
    bids, asks = book.get("bids") or [], book.get("asks") or []
    bid = float(bids[0][0]) if bids else None
    ask = float(asks[0][0]) if asks else None
    out: dict[str, Any] = {"bid": bid, "ask": ask}
    if bid is None or ask is None:
        return {**out, "status": "error", "error": "empty book"}
    mid = (bid + ask) / 2
    vwap, filled, used = walk_book(asks if side == "buy" else bids, notional)
    sgn = 1.0 if side == "buy" else -1.0
    sf_mid = sgn * (vwap / mid - 1.0) * 1e4 if vwap else None
    sf_close = sgn * (vwap / ledger_price - 1.0) * 1e4 if (vwap and ledger_price) else None
    return {**out, "mid": mid, "spread_bp": (ask - bid) / mid * 1e4, "vwap": vwap, "filled_notional": filled, "levels": used,
            "shortfall_mid_bp": sf_mid, "shortfall_close_bp": sf_close,
            "deviation_bp": (sf_mid - assumed_slip * 1e4) if sf_mid is not None else None,
            "status": "ok" if filled >= notional * (1 - 1e-6) else "partial", "error": None}


class ExecShadowLedger:
    def __init__(self, path: Optional[Path | str] = None):
        self.path = Path(path) if path else data_dir() / "exec_shadow.sqlite"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None, timeout=30)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(_SCHEMA)
        self._lock = threading.RLock()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def put(self, row: dict) -> None:
        with self._lock:
            self._db.execute(f"INSERT OR IGNORE INTO quotes({','.join(row)}) VALUES ({','.join('?' * len(row))})", tuple(row.values()))

    def rows(self, limit: int = 2000) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._db.execute("SELECT * FROM quotes ORDER BY day DESC, coin LIMIT ?", (limit,))]


BookFn = Callable[[str], tuple[Optional[str], Optional[str], dict]]  # coin -> (exchange, symbol, book)


def record(led: ExecShadowLedger, day: int, fills: list[dict], *, catchup: bool, book_fn: BookFn,
           cost: Optional[CostModel] = None, now_ms: Callable[[], int] = lambda: int(time.time() * 1000)) -> int:
    cost = cost or CostModel()
    n = 0
    for f in fills:
        c = f["coin"]
        slip = cost.slippage.get(c, cost.slippage_default) * cost.mult
        base = {"day": day, "coin": c, "side": f["side"], "notional": float(f["notional"]),
                "ledger_price": float(f["price"]) if f.get("price") else None, "assumed_slip": slip}
        if catchup:
            led.put({**base, "status": "skipped_catchup", "ts": now_ms()})
            continue
        try:
            ex, sym, book = book_fn(c)
            m = measure(book, f["side"], float(f["notional"]), base["ledger_price"], slip)
            led.put({**base, **m, "ts": now_ms(), "exchange": ex, "symbol": sym})
        except Exception as exc:  # never let a public-API hiccup touch the runner
            led.put({**base, "status": "error", "ts": now_ms(), "error": f"{type(exc).__name__}: {exc}"[:200]})
            log.warning("exec shadow %s %s failed: %s", ms_day(day), c, exc)
        n += 1
    return n


def summarize(rows: list[dict]) -> dict:
    ok = [r for r in rows if r["status"] in ("ok", "partial") and r["shortfall_mid_bp"] is not None]
    per: dict[str, dict] = {}
    for r in ok:
        p = per.setdefault(r["coin"], {"n": 0, "spread": 0.0, "mid": 0.0, "close": 0.0, "dev": 0.0, "notional": 0.0, "assumed": r["assumed_slip"] * 1e4, "nc": 0})
        p["n"] += 1
        p["notional"] += r["notional"]
        p["spread"] += r["spread_bp"]
        p["mid"] += r["shortfall_mid_bp"] * r["notional"]
        p["dev"] += r["deviation_bp"] * r["notional"]
        if r["shortfall_close_bp"] is not None:
            p["close"] += r["shortfall_close_bp"] * r["notional"]
            p["nc"] += r["notional"]
    coins = []
    for c, p in sorted(per.items()):
        coins.append({"coin": c, "n": p["n"], "notional": p["notional"], "spreadBp": p["spread"] / p["n"],
                      "shortfallMidBp": p["mid"] / p["notional"], "shortfallCloseBp": (p["close"] / p["nc"]) if p["nc"] else None,
                      "assumedBp": p["assumed"], "deviationBp": p["dev"] / p["notional"]})
    tot = sum(r["notional"] for r in ok)
    return {
        "ledger": "exec_shadow.sqlite (separate; never changes fills)",
        "n": len(ok), "skipped": sum(1 for r in rows if r["status"] == "skipped_catchup"),
        "errors": sum(1 for r in rows if r["status"] == "error"), "partial": sum(1 for r in rows if r["status"] == "partial"),
        "notionalWeightedDeviationBp": (sum(r["deviation_bp"] * r["notional"] for r in ok) / tot) if tot else None,
        "notionalWeightedShortfallMidBp": (sum(r["shortfall_mid_bp"] * r["notional"] for r in ok) / tot) if tot else None,
        "coins": coins,
        "recent": rows[:40],
        "note": "偏差 = 按盘口 VWAP 相对中间价的成本 − 假设滑点；> 0 表示假设偏乐观。对收盘价口径还包含收盘到快照之间的价格漂移。手续费两边相同，不计入。",
    }


# ---- production wiring -----------------------------------------------------------------
_led: Optional[ExecShadowLedger] = None
_q: "queue.Queue[tuple[int, list[dict], bool]]" = queue.Queue()
_worker: Optional[threading.Thread] = None
_wlock = threading.Lock()


def get_ledger() -> ExecShadowLedger:
    global _led
    with _wlock:
        if _led is None:
            _led = ExecShadowLedger()
        return _led


def _book(coin: str) -> tuple[Optional[str], Optional[str], dict]:
    from app.marketdata.mainstream import get_service

    svc = get_service()
    ex = svc.exchange_for_read()
    if not ex:
        raise RuntimeError("no exchange")
    sym = svc.cfg.perp(coin)
    f = svc._od_fetcher(ex)
    book = f.call("fetch_order_book", sym, BOOK_LEVELS)
    return ex, sym, to_base(book, contract_size(f.client, sym))


def contract_size(client, symbol: str) -> float:
    """Base units per contract (OKX swaps quote book sizes in contracts; Binance USD-M in base)."""
    try:
        m = client.market(symbol)
    except Exception:
        return 1.0
    cs = m.get("contractSize") if isinstance(m, dict) else None
    return float(cs) if cs else 1.0


def to_base(book: dict, cs: float) -> dict:
    if cs == 1.0:
        return book
    return {**book, "bids": [[p, q * cs] for p, q, *_ in book.get("bids") or []],
            "asks": [[p, q * cs] for p, q, *_ in book.get("asks") or []], "contractSize": cs}


def _run() -> None:
    while True:
        day, fills, catchup = _q.get()
        try:
            record(get_ledger(), day, fills, catchup=catchup, book_fn=_book)
            log.info("exec shadow recorded %s (%d fills%s)", ms_day(day), len(fills), ", catch-up" if catchup else "")
        except Exception:
            log.exception("exec shadow")


def on_commit(day: int, fills: list[dict], catchup: bool) -> None:
    """Runner hook (after the ledger commit): queue the snapshot on a background thread."""
    global _worker
    if not fills:
        return
    with _wlock:
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_run, name="exec-shadow", daemon=True)
            _worker.start()
    _q.put((day, [dict(f) for f in fills], catchup))


def peek_summary() -> Optional[dict]:
    if _led is None and not (data_dir() / "exec_shadow.sqlite").exists():
        return None
    return summarize(get_ledger().rows())
