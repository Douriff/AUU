"""Per-user mainstream paper trading account (spot-style, long-only, USDT quoted).

Paper only: nothing here can reach an exchange order endpoint. Each logged-in user has
their own account in ``data_dir()/mainstream_paper.sqlite`` (the new mainstream paper
ledger). Market orders fill at the latest public price +/- slippage and pay the taker fee
from :class:`app.backtest.costs.CostModel` (the same cost model the backtest uses). Limit
orders rest until a 1m candle trades through the limit, then fill at the limit price and
pay the maker fee.

Take-profit / stop-loss (manual paper only; the automated strategy never uses them): sell-side
trigger orders on an existing long position. A take-profit triggers when a 1m bar's high reaches
the trigger, a stop-loss when the low reaches it; the fill is a market sell at the trigger (or at
the bar open when the bar gapped through it) minus slippage, taker fee. Two legs placed together
form an OCO pair: when one fills the other is cancelled, and the pair reserves the quantity once.
If one bar touches both legs, the stop-loss is assumed first (conservative). Triggers are checked
on the same lazy pass as limit orders, from the same 1m candles (no extra exchange requests);
the bar the order was placed in is skipped so earlier prices in that minute cannot trigger it.

Risk controls: per-order notional cap, per-symbol position cap, cash/position checks,
max open orders, orders-per-minute limit, idempotent ``client_order_id`` and an
identical-order window against double submits, stale-price refusal.
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from app.backtest.costs import CostModel
from app.data_paths import data_dir

MAKER_FEE = 0.0002

_SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
  user_id TEXT PRIMARY KEY, cash REAL NOT NULL, start_cash REAL NOT NULL, created_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS positions (
  user_id TEXT NOT NULL, symbol TEXT NOT NULL, qty REAL NOT NULL, avg_px REAL NOT NULL,
  realized REAL NOT NULL DEFAULT 0, fees REAL NOT NULL DEFAULT 0,
  PRIMARY KEY (user_id, symbol)
);
CREATE TABLE IF NOT EXISTS orders (
  id TEXT PRIMARY KEY, user_id TEXT NOT NULL, client_id TEXT NOT NULL, ts INTEGER NOT NULL,
  symbol TEXT NOT NULL, side TEXT NOT NULL, type TEXT NOT NULL, qty REAL NOT NULL,
  limit_px REAL, status TEXT NOT NULL, fill_px REAL, fill_ts INTEGER, fee REAL DEFAULT 0,
  slippage REAL DEFAULT 0, notional REAL DEFAULT 0, reserved REAL DEFAULT 0, ref_px REAL,
  reason TEXT, realized REAL DEFAULT 0,
  UNIQUE (user_id, client_id)
);
CREATE INDEX IF NOT EXISTS orders_user_ts ON orders (user_id, ts);
"""
TRIGGER_TYPES = ("take_profit", "stop_loss")


def _f(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "") or default)
    except ValueError:
        return default


@dataclass(frozen=True)
class Limits:
    start_cash: float = 10_000.0
    max_order_usdt: float = 2_000.0
    max_position_usdt: float = 5_000.0
    min_order_usdt: float = 10.0
    max_open_orders: int = 20
    orders_per_min: int = 20
    dup_window_ms: int = 3_000
    max_price_age_ms: int = 180_000
    limit_band: float = 0.5  # limit price must be within +/-50% of the last price

    @classmethod
    def from_env(cls) -> "Limits":
        return cls(
            start_cash=_f("AUU_PAPER_START_USDT", 10_000.0),
            max_order_usdt=_f("AUU_PAPER_MAX_ORDER_USDT", 2_000.0),
            max_position_usdt=_f("AUU_PAPER_MAX_POSITION_USDT", 5_000.0),
            max_open_orders=int(_f("AUU_PAPER_MAX_OPEN_ORDERS", 20)),
            orders_per_min=int(_f("AUU_PAPER_ORDERS_PER_MIN", 20)),
        )


class OrderError(Exception):
    """`key`/`params` let the web UI show the message in the user's language (message stays zh)."""

    def __init__(self, code: str, message: str, status: int = 400, key: Optional[str] = None, params: Optional[dict] = None):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status
        self.key, self.params = key, params or {}

    def extra(self) -> Optional[dict]:
        return {"key": self.key, "params": self.params} if self.key else None


# price_fn(symbol) -> (price, ts_ms) or None ; candles_fn(symbol, since_ms) -> [{ts, high, low, open, close}]
PriceFn = Callable[[str], Optional[tuple[float, int]]]
CandlesFn = Callable[[str, int], list[dict]]


class PaperAccounts:
    def __init__(
        self,
        path: Optional[Path | str] = None,
        *,
        price_fn: PriceFn,
        candles_fn: CandlesFn,
        symbols: Callable[[], list[str]],
        limits: Optional[Limits] = None,
        cost: Optional[CostModel] = None,
        now_ms: Callable[[], int] = lambda: int(time.time() * 1000),
    ):
        self.path = Path(path) if path else data_dir() / "mainstream_paper.sqlite"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.price_fn, self.candles_fn, self.symbols = price_fn, candles_fn, symbols
        self.limits = limits or Limits.from_env()
        self.cost = cost or CostModel()
        self.now_ms = now_ms
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(self.path), check_same_thread=False, timeout=30)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.executescript(_SCHEMA)
            cols = {r[1] for r in self._db.execute("PRAGMA table_info(orders)")}
            for c, t in (("trigger_px", "REAL"), ("oco_group", "TEXT"), ("checked_ts", "INTEGER")):  # TP/SL (older rows NULL)
                if c not in cols:
                    self._db.execute(f"ALTER TABLE orders ADD COLUMN {c} {t}")
            self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # ---- helpers ---------------------------------------------------------
    def _account(self, uid: str) -> sqlite3.Row:
        row = self._db.execute("SELECT * FROM accounts WHERE user_id=?", (uid,)).fetchone()
        if row is None:
            self._db.execute(
                "INSERT INTO accounts VALUES (?,?,?,?)", (uid, self.limits.start_cash, self.limits.start_cash, self.now_ms())
            )
            row = self._db.execute("SELECT * FROM accounts WHERE user_id=?", (uid,)).fetchone()
        return row

    def _position(self, uid: str, sym: str) -> dict:
        row = self._db.execute("SELECT * FROM positions WHERE user_id=? AND symbol=?", (uid, sym)).fetchone()
        return dict(row) if row else {"user_id": uid, "symbol": sym, "qty": 0.0, "avg_px": 0.0, "realized": 0.0, "fees": 0.0}

    def _save_position(self, p: dict) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO positions VALUES (?,?,?,?,?,?)",
            (p["user_id"], p["symbol"], p["qty"], p["avg_px"], p["realized"], p["fees"]),
        )

    def _reserved(self, uid: str) -> tuple[float, dict[str, float]]:
        cash, qty = 0.0, {}
        groups: dict[str, tuple[str, float]] = {}
        for r in self._db.execute("SELECT side, symbol, reserved, qty, oco_group FROM orders WHERE user_id=? AND status='open'", (uid,)):
            if r["side"] == "buy":
                cash += r["reserved"]
            elif r["oco_group"]:  # both legs of an OCO pair reserve the quantity once
                prev = groups.get(r["oco_group"])
                groups[r["oco_group"]] = (r["symbol"], max(r["qty"], prev[1] if prev else 0.0))
            else:
                qty[r["symbol"]] = qty.get(r["symbol"], 0.0) + r["qty"]
        for sym, q in groups.values():
            qty[sym] = qty.get(sym, 0.0) + q
        return cash, qty

    def _price(self, sym: str) -> tuple[float, int]:
        got = self.price_fn(sym)
        if not got or not got[0]:
            raise OrderError("NO_PRICE", "暂时拿不到行情价格，稍后再试", 503)
        px, ts = float(got[0]), int(got[1])
        if self.now_ms() - ts > self.limits.max_price_age_ms:
            raise OrderError("STALE_PRICE", "行情价格已过期，暂停下单", 503)
        return px, ts

    def slippage(self, sym: str) -> float:
        return self.cost.slippage.get(sym, self.cost.slippage_default) * self.cost.mult

    def costs(self, sym: str) -> dict:
        return {"takerFee": self.cost.taker * self.cost.mult, "makerFee": MAKER_FEE, "slippage": self.slippage(sym)}

    # ---- fills -------------------------------------------------------------
    def _apply_fill(self, uid: str, order: dict, px: float, fee_rate: float, slip: float, ts: int) -> None:
        sym, side, qty = order["symbol"], order["side"], order["qty"]
        notional = qty * px
        fee = notional * fee_rate
        acct = self._account(uid)
        pos = self._position(uid, sym)
        realized = 0.0
        if side == "buy":
            new_qty = pos["qty"] + qty
            pos["avg_px"] = (pos["avg_px"] * pos["qty"] + notional) / new_qty
            pos["qty"] = new_qty
            cash = acct["cash"] - notional - fee
        else:
            realized = (px - pos["avg_px"]) * qty - fee
            pos["qty"] = max(0.0, pos["qty"] - qty)
            if pos["qty"] < 1e-12:
                pos["qty"], pos["avg_px"] = 0.0, 0.0
            cash = acct["cash"] + notional - fee
        pos["realized"] += realized if side == "sell" else -fee
        pos["fees"] += fee
        self._save_position(pos)
        self._db.execute("UPDATE accounts SET cash=? WHERE user_id=?", (cash, uid))
        self._db.execute(
            "UPDATE orders SET status='filled', fill_px=?, fill_ts=?, fee=?, slippage=?, notional=?, reserved=0, realized=? WHERE id=?",
            (px, ts, fee, slip * qty * (order.get("ref_px") or px), notional, realized, order["id"]),
        )

    def match_open(self, uid: str) -> int:
        """Fill resting limit orders that the market traded through since placement, then TP/SL triggers."""
        n = 0
        with self._lock:
            rows = [dict(r) for r in self._db.execute("SELECT * FROM orders WHERE user_id=? AND status='open' ORDER BY ts", (uid,))]
            for o in rows:
                if o["type"] in TRIGGER_TYPES:
                    continue
                try:
                    bars = self.candles_fn(o["symbol"], o["ts"])
                except Exception:
                    continue
                for b in bars:
                    if int(b["ts"]) + 60_000 <= o["ts"]:
                        continue  # bar closed before the order existed
                    hit = (o["side"] == "buy" and b["low"] <= o["limit_px"]) or (o["side"] == "sell" and b["high"] >= o["limit_px"])
                    if not hit:
                        continue
                    pos = self._position(uid, o["symbol"])
                    if o["side"] == "sell" and pos["qty"] + 1e-12 < o["qty"]:
                        self._db.execute("UPDATE orders SET status='cancelled', reserved=0, reason='INSUFFICIENT_POSITION' WHERE id=?", (o["id"],))
                    else:
                        self._apply_fill(uid, o, o["limit_px"], MAKER_FEE, 0.0, max(int(b["ts"]), o["ts"]))
                        n += 1
                    break
            n += self._match_triggers(uid, [o for o in rows if o["type"] in TRIGGER_TYPES])
            self._db.commit()
        return n

    @staticmethod
    def _scan_from(o: dict) -> int:
        """First 1m bar to check: the minute after placement, or after the last bar already checked."""
        start = (int(o["ts"]) // 60_000 + 1) * 60_000
        if o.get("checked_ts") is not None:
            start = max(start, int(o["checked_ts"]) + 60_000)
        return start

    @staticmethod
    def _first_hit(o: dict, bars: list[dict], start: int) -> Optional[tuple[int, float]]:
        """(bar ts, raw fill price) of the first 1m bar from ``start`` that reaches the trigger."""
        trig = float(o["trigger_px"])
        for b in bars:
            ts = int(b["ts"])
            if ts < start:
                continue
            if o["type"] == "take_profit" and float(b["high"]) >= trig:
                return ts, max(trig, float(b["open"]))
            if o["type"] == "stop_loss" and float(b["low"]) <= trig:
                return ts, min(trig, float(b["open"]))
        return None

    def _match_triggers(self, uid: str, rows: list[dict]) -> int:
        n = 0
        cache: dict[tuple[str, int], Optional[list[dict]]] = {}  # one local candle read per (symbol, start) per pass

        def bars_for(sym: str, start: int) -> Optional[list[dict]]:
            if (sym, start) not in cache:
                try:
                    cache[(sym, start)] = self.candles_fn(sym, start)
                except Exception:
                    cache[(sym, start)] = None
            return cache[(sym, start)]

        groups: dict[str, list[dict]] = {}
        for o in rows:
            groups.setdefault(o["oco_group"] or o["id"], []).append(o)
        for legs in groups.values():
            hits, scanned = [], []
            for o in legs:
                start = self._scan_from(o)
                bars = bars_for(o["symbol"], start)
                if not bars:
                    continue
                h = self._first_hit(o, bars, start)
                if h is not None:
                    hits.append((h[0], 0 if o["type"] == "stop_loss" else 1, h[1], o))
                else:
                    # remember progress so long-resting orders keep moving forward; the newest bar may
                    # still be forming, so it is checked again next time
                    done = max(int(b["ts"]) for b in bars) - 60_000
                    if done >= start:
                        scanned.append((done, o["id"]))
            if not hits:
                for done, oid in scanned:
                    self._db.execute("UPDATE orders SET checked_ts=? WHERE id=?", (done, oid))
                continue
            ts, _, raw, o = min(hits, key=lambda x: (x[0], x[1]))  # earliest bar; same bar -> stop-loss first
            pos = self._position(uid, o["symbol"])
            if pos["qty"] + 1e-12 < o["qty"]:
                self._db.execute("UPDATE orders SET status='cancelled', reserved=0, reason='INSUFFICIENT_POSITION' WHERE id=?", (o["id"],))
            else:
                slip = self.slippage(o["symbol"])
                o = {**o, "ref_px": raw}
                self._apply_fill(uid, o, raw * (1 - slip), self.cost.taker * self.cost.mult, slip, max(ts, int(o["ts"])))
                self._db.execute("UPDATE orders SET reason=? WHERE id=?", ("TP_TRIGGERED" if o["type"] == "take_profit" else "SL_TRIGGERED", o["id"]))
                n += 1
            for other in legs:
                if other["id"] != o["id"]:
                    self._db.execute("UPDATE orders SET status='cancelled', reserved=0, reason='OCO_CANCEL' WHERE id=? AND status='open'", (other["id"],))
        return n

    # ---- orders ------------------------------------------------------------
    def place(self, uid: str, body: dict) -> dict:
        lim = self.limits
        sym = "".join(ch for ch in str(body.get("symbol", "")).split("/")[0].upper() if ch.isalnum())
        if sym not in self.symbols():
            raise OrderError("UNKNOWN_SYMBOL", f"不支持的币种：{body.get('symbol')}", 404, key="unknownSymbol", params={"s": str(body.get('symbol'))[:20]})
        side, otype = body.get("side"), body.get("type", "market")
        if otype in TRIGGER_TYPES:
            return self.place_tpsl(uid, {**body, otype: body.get("trigger_price")}, only=otype)
        if otype == "oco":
            return self.place_tpsl(uid, {**body, "take_profit": body.get("take_profit_price"), "stop_loss": body.get("stop_loss_price")})
        if side not in ("buy", "sell") or otype not in ("market", "limit"):
            raise OrderError("BAD_ORDER", "side 必须是 buy/sell，type 必须是 market/limit/take_profit/stop_loss", key="badSideType")
        cid = str(body.get("client_order_id") or "").strip()
        if not (8 <= len(cid) <= 64) or not all(ch.isalnum() or ch in "-_" for ch in cid):
            raise OrderError("BAD_CLIENT_ID", "缺少有效的 client_order_id（防重复提交）")
        now = self.now_ms()
        with self._lock:
            prev = self._db.execute("SELECT * FROM orders WHERE user_id=? AND client_id=?", (uid, cid)).fetchone()
            if prev is not None:
                return {**self._public(dict(prev)), "duplicate": True}
            self._account(uid)
            recent = self._db.execute("SELECT COUNT(*) FROM orders WHERE user_id=? AND ts>?", (uid, now - 60_000)).fetchone()[0]
            if recent >= lim.orders_per_min:
                raise OrderError("RATE_LIMIT", "下单太频繁，请稍后再试", 429)
            px, px_ts = self._price(sym)
            limit_px = None
            if otype == "limit":
                try:
                    limit_px = float(body.get("limit_price"))
                except (TypeError, ValueError):
                    raise OrderError("BAD_PRICE", "限价单需要有效的限价", key="limitRequired")
                if not (limit_px > 0) or abs(limit_px / px - 1) > lim.limit_band:
                    raise OrderError("BAD_PRICE", "限价偏离现价太多（±50% 以内）", key="limitTooFar")
            ref = limit_px or px
            qty = body.get("qty")
            notional_in = body.get("notional")
            try:
                if qty not in (None, ""):
                    qty = float(qty)
                elif notional_in not in (None, ""):
                    qty = float(notional_in) / ref
                else:
                    raise OrderError("BAD_QTY", "请填写数量或金额", key="qtyRequired")
            except ValueError:
                raise OrderError("BAD_QTY", "数量或金额格式不正确", key="qtyFormat")
            qty = round(qty, 8)
            if not (qty > 0):
                raise OrderError("BAD_QTY", "数量必须大于 0", key="qtyPositive")
            notional = qty * ref
            if notional < lim.min_order_usdt:
                raise OrderError("ORDER_TOO_SMALL", f"单笔至少 {lim.min_order_usdt:g} USDT", key="orderMin", params={"v": f"{lim.min_order_usdt:g}"})
            if notional > lim.max_order_usdt + 1e-9:
                raise OrderError("ORDER_CAP", f"单笔上限 {lim.max_order_usdt:g} USDT", 422, key="orderMax", params={"v": f"{lim.max_order_usdt:g}"})
            dup = self._db.execute(
                "SELECT id FROM orders WHERE user_id=? AND symbol=? AND side=? AND type=? AND ABS(qty-?)<1e-9 AND ts>? AND status!='rejected'",
                (uid, sym, side, otype, qty, now - lim.dup_window_ms),
            ).fetchone()
            if dup is not None:
                raise OrderError("DUPLICATE_ORDER", "相同的订单刚刚提交过，请确认后再下", 409)
            res_cash, res_qty = self._reserved(uid)
            acct = self._account(uid)
            pos = self._position(uid, sym)
            fee_rate = self.cost.taker * self.cost.mult if otype == "market" else MAKER_FEE
            slip = self.slippage(sym) if otype == "market" else 0.0
            fill_px = px * (1 + slip) if side == "buy" else px * (1 - slip)
            exec_px = fill_px if otype == "market" else limit_px
            if side == "buy":
                need = qty * exec_px * (1 + fee_rate)
                if need > acct["cash"] - res_cash + 1e-9:
                    raise OrderError("INSUFFICIENT_CASH", "可用资金不足", 422)
                pending_buy = sum(
                    r["qty"] * r["limit_px"] for r in self._db.execute(
                        "SELECT qty, limit_px FROM orders WHERE user_id=? AND symbol=? AND side='buy' AND status='open'", (uid, sym))
                )
                if (pos["qty"] * px) + pending_buy + qty * exec_px > lim.max_position_usdt + 1e-9:
                    raise OrderError("POSITION_CAP", f"单币持仓上限 {lim.max_position_usdt:g} USDT", 422, key="positionCap", params={"v": f"{lim.max_position_usdt:g}"})
            else:
                if qty > pos["qty"] - res_qty.get(sym, 0.0) + 1e-12:
                    raise OrderError("INSUFFICIENT_POSITION", "可卖数量不足（只做现货多头，不能做空）", 422, key="noShort")
            if otype == "limit":
                open_n = self._db.execute("SELECT COUNT(*) FROM orders WHERE user_id=? AND status='open'", (uid,)).fetchone()[0]
                if open_n >= lim.max_open_orders:
                    raise OrderError("TOO_MANY_OPEN", f"挂单最多 {lim.max_open_orders} 个", 422, key="tooManyOpen", params={"n": lim.max_open_orders})
            order = {
                "id": uuid.uuid4().hex[:16], "user_id": uid, "client_id": cid, "ts": now, "symbol": sym, "side": side,
                "type": otype, "qty": qty, "limit_px": limit_px, "status": "open", "ref_px": px,
                "reserved": qty * limit_px * (1 + MAKER_FEE) if (otype == "limit" and side == "buy") else 0.0,
            }
            self._db.execute(
                "INSERT INTO orders (id,user_id,client_id,ts,symbol,side,type,qty,limit_px,status,reserved,ref_px) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                tuple(order[k] for k in ("id", "user_id", "client_id", "ts", "symbol", "side", "type", "qty", "limit_px", "status", "reserved", "ref_px")),
            )
            if otype == "market":
                self._apply_fill(uid, order, fill_px, fee_rate, slip, now)
            else:
                marketable = (side == "buy" and limit_px >= px) or (side == "sell" and limit_px <= px)
                if marketable:  # crosses the market: fills now as a taker, never worse than the limit
                    tslip = self.slippage(sym)
                    tpx = min(limit_px, px * (1 + tslip)) if side == "buy" else max(limit_px, px * (1 - tslip))
                    self._apply_fill(uid, order, tpx, self.cost.taker * self.cost.mult, tslip, now)
            self._db.commit()
            row = self._db.execute("SELECT * FROM orders WHERE id=?", (order["id"],)).fetchone()
            return {**self._public(dict(row)), "duplicate": False}

    def cancel(self, uid: str, order_id: str) -> dict:
        with self._lock:
            row = self._db.execute("SELECT * FROM orders WHERE id=? AND user_id=?", (order_id, uid)).fetchone()
            if row is None:
                raise OrderError("NOT_FOUND", "没有这个订单", 404)
            if row["status"] != "open":
                raise OrderError("NOT_OPEN", "订单已成交或已撤销", 409)
            self._db.execute("UPDATE orders SET status='cancelled', reserved=0, reason='USER_CANCEL' WHERE id=?", (order_id,))
            if row["oco_group"]:  # the pair goes together
                self._db.execute("UPDATE orders SET status='cancelled', reserved=0, reason='OCO_CANCEL' WHERE oco_group=? AND user_id=? AND status='open'",
                                 (row["oco_group"], uid))
            self._db.commit()
            return self._public(dict(self._db.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()))

    def place_tpsl(self, uid: str, body: dict, only: Optional[str] = None) -> dict:
        """Take-profit and/or stop-loss sell for an existing long. Both given -> OCO pair (shared quantity).

        Returns the first leg in the usual order shape plus ``orders`` (all legs) and ``ocoGroup``."""
        lim = self.limits
        sym = "".join(ch for ch in str(body.get("symbol", "")).split("/")[0].upper() if ch.isalnum())
        if sym not in self.symbols():
            raise OrderError("UNKNOWN_SYMBOL", f"不支持的币种：{body.get('symbol')}", 404, key="unknownSymbol", params={"s": str(body.get('symbol'))[:20]})
        if body.get("side", "sell") != "sell":
            raise OrderError("BAD_ORDER", "止盈止损只用于卖出已有持仓（只做现货多头）", key="tpslSellOnly")
        cid = str(body.get("client_order_id") or "").strip()
        if not (8 <= len(cid) <= 60) or not all(ch.isalnum() or ch in "-_" for ch in cid):
            raise OrderError("BAD_CLIENT_ID", "缺少有效的 client_order_id（防重复提交）")
        legs: dict[str, float] = {}
        for k in ((only,) if only else TRIGGER_TYPES):
            v = body.get(k)
            if v in (None, ""):
                continue
            try:
                legs[k] = float(v)
            except (TypeError, ValueError):
                raise OrderError("BAD_PRICE", "触发价格式不正确", key="triggerFormat")
            if not (legs[k] > 0):
                raise OrderError("BAD_PRICE", "触发价必须大于 0", key="triggerPositive")
        if not legs or (not only and len(legs) != 2):
            raise OrderError("BAD_PRICE", "请填写触发价" if only else "OCO 需要同时填写止盈价和止损价", key="triggerRequired" if only else "ocoBoth")
        now = self.now_ms()
        with self._lock:
            prev = [dict(r) for r in self._db.execute("SELECT * FROM orders WHERE user_id=? AND client_id IN (?,?)",
                                                        (uid, f"{cid}-tp", f"{cid}-sl"))]
            if prev:
                pub = [self._public(o) for o in prev]
                return {**pub[0], "orders": pub, "ocoGroup": prev[0]["oco_group"], "duplicate": True}
            self._account(uid)
            recent = self._db.execute("SELECT COUNT(*) FROM orders WHERE user_id=? AND ts>?", (uid, now - 60_000)).fetchone()[0]
            if recent + len(legs) > lim.orders_per_min:
                raise OrderError("RATE_LIMIT", "下单太频繁，请稍后再试", 429)
            px, _ = self._price(sym)
            tp, sl = legs.get("take_profit"), legs.get("stop_loss")
            if tp is not None and not (tp > px):
                raise OrderError("BAD_PRICE", "止盈价必须高于现价（否则会立即触发）", key="tpAbove")
            if sl is not None and not (sl < px):
                raise OrderError("BAD_PRICE", "止损价必须低于现价（否则会立即触发）", key="slBelow")
            if any(abs(v / px - 1) > lim.limit_band for v in legs.values()):
                raise OrderError("BAD_PRICE", "触发价偏离现价太多（±50% 以内）", key="triggerTooFar")
            try:
                if body.get("qty") not in (None, ""):
                    qty = float(body["qty"])
                elif body.get("notional") not in (None, ""):
                    qty = float(body["notional"]) / px
                else:
                    raise OrderError("BAD_QTY", "请填写数量或金额", key="qtyRequired")
            except ValueError:
                raise OrderError("BAD_QTY", "数量或金额格式不正确", key="qtyFormat")
            qty = round(qty, 8)
            if not (qty > 0):
                raise OrderError("BAD_QTY", "数量必须大于 0", key="qtyPositive")
            for v in legs.values():  # same per-order caps as every other order, at each trigger price
                if qty * v < lim.min_order_usdt:
                    raise OrderError("ORDER_TOO_SMALL", f"单笔至少 {lim.min_order_usdt:g} USDT", key="orderMin", params={"v": f"{lim.min_order_usdt:g}"})
                if qty * v > lim.max_order_usdt + 1e-9:
                    raise OrderError("ORDER_CAP", f"单笔上限 {lim.max_order_usdt:g} USDT", 422, key="orderMax", params={"v": f"{lim.max_order_usdt:g}"})
            _, res_qty = self._reserved(uid)
            pos = self._position(uid, sym)
            if qty > pos["qty"] - res_qty.get(sym, 0.0) + 1e-12:
                raise OrderError("INSUFFICIENT_POSITION", "可卖数量不足（止盈止损只能保护已有持仓）", 422, key="tpslHeldOnly")
            open_n = self._db.execute("SELECT COUNT(*) FROM orders WHERE user_id=? AND status='open'", (uid,)).fetchone()[0]
            if open_n + len(legs) > lim.max_open_orders:
                raise OrderError("TOO_MANY_OPEN", f"挂单最多 {lim.max_open_orders} 个", 422, key="tooManyOpen", params={"n": lim.max_open_orders})
            group = uuid.uuid4().hex[:12] if len(legs) == 2 else None
            ids = []
            for k, v in legs.items():
                oid = uuid.uuid4().hex[:16]
                ids.append(oid)
                self._db.execute(
                    "INSERT INTO orders (id,user_id,client_id,ts,symbol,side,type,qty,limit_px,status,reserved,ref_px,trigger_px,oco_group)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (oid, uid, f"{cid}-{'tp' if k == 'take_profit' else 'sl'}", now, sym, "sell", k, qty, None, "open", 0.0, px, v, group))
            self._db.commit()
            rows = [dict(self._db.execute("SELECT * FROM orders WHERE id=?", (i,)).fetchone()) for i in ids]
        pub = [self._public(o) for o in rows]
        return {**pub[0], "orders": pub, "ocoGroup": group, "duplicate": False}

    # ---- close position (market, at the same price the P&L header uses) -------------------------
    @staticmethod
    def _norm_sym(symbol: Any) -> str:
        return "".join(ch for ch in str(symbol or "").split("/")[0].upper() if ch.isalnum())

    @staticmethod
    def _close_cid(raw: Any) -> str:
        cid = str(raw or "").strip()
        if not (8 <= len(cid) <= 64) or not all(ch.isalnum() or ch in "-_" for ch in cid):
            raise OrderError("BAD_CLIENT_ID", "缺少有效的 client_order_id（防重复提交）", key="badClientId")
        return cid

    def _close_locked(self, uid: str, sym: str, cid_full: str, px: float, now: int) -> dict:
        """Sell the whole position of ``sym`` at ``px`` (minus slippage, taker fee) and return the order row.

        Caller holds ``self._lock`` and an open write transaction. Resting sell-side orders of the coin
        (limit sells, TP/SL, OCO) are cancelled first because they reserve the quantity being sold."""
        pos = self._position(uid, sym)
        qty = float(pos["qty"])
        self._db.execute(
            "UPDATE orders SET status='cancelled', reserved=0, reason='CLOSE_CANCEL' "
            "WHERE user_id=? AND symbol=? AND side='sell' AND status='open'", (uid, sym))
        slip = self.slippage(sym)
        fill_px = px * (1 - slip)
        order = {"id": uuid.uuid4().hex[:16], "user_id": uid, "client_id": cid_full, "ts": now, "symbol": sym,
                 "side": "sell", "type": "market", "qty": qty, "limit_px": None, "status": "open", "ref_px": px, "reserved": 0.0}
        self._db.execute(
            "INSERT INTO orders (id,user_id,client_id,ts,symbol,side,type,qty,limit_px,status,reserved,ref_px) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            tuple(order[k] for k in ("id", "user_id", "client_id", "ts", "symbol", "side", "type", "qty", "limit_px", "status", "reserved", "ref_px")))
        self._apply_fill(uid, order, fill_px, self.cost.taker * self.cost.mult, slip, now)
        self._db.execute("UPDATE orders SET reason='CLOSE' WHERE id=?", (order["id"],))
        return dict(self._db.execute("SELECT * FROM orders WHERE id=?", (order["id"],)).fetchone())

    def _close_many(self, uid: str, client_order_id: Any, symbols: Optional[list[str]]) -> dict:
        """Market-close ``symbols`` (None = every open position) of ``uid`` only.

        Idempotent per ``(user, client_order_id, symbol)``: the order row uses the unique
        ``(user_id, client_id)`` key ``<cid>-x-<SYMBOL>``, so a repeat request returns the earlier fill
        (``duplicate: true``) and never sells twice. The whole batch runs in one ``BEGIN IMMEDIATE``
        transaction under the account lock, so concurrent requests (threads or processes) serialise and
        the loser finds the position already flat. Strategy accounts / shadow books are other files."""
        cid = self._close_cid(client_order_id)
        with self._lock:
            try:
                self.match_open(uid)  # settle resting limit / TP / SL fills first so the quantity is current
            except Exception:
                pass
            self._db.commit()
            held = [r["symbol"] for r in self._db.execute(
                "SELECT symbol FROM positions WHERE user_id=? AND qty>1e-12 ORDER BY symbol", (uid,))]
            if symbols is None:  # replay of an earlier close-all also lists the coins it already closed
                pre = f"{cid}-x-"
                done = [r["client_id"][len(pre):] for r in self._db.execute(
                    "SELECT client_id FROM orders WHERE user_id=? AND substr(client_id,1,?)=?", (uid, len(pre), pre))]
                wanted = sorted(set(held) | set(done))
            else:
                wanted = symbols
            prices: dict[str, tuple[float, int]] = {}
            failed: list[dict] = []
            for sym in wanted:
                if self._db.execute("SELECT 1 FROM orders WHERE user_id=? AND client_id=?", (uid, f"{cid}-x-{sym}")).fetchone():
                    continue  # replay: nothing to price
                if sym not in held:
                    continue
                try:
                    prices[sym] = self._price(sym)
                except OrderError as e:
                    failed.append({"symbol": sym, "code": e.code, "message": e.message, "key": e.key})
            now = self.now_ms()
            closed: list[dict] = []
            dup = False
            self._db.execute("BEGIN IMMEDIATE")
            try:
                for sym in wanted:
                    prev = self._db.execute("SELECT * FROM orders WHERE user_id=? AND client_id=?", (uid, f"{cid}-x-{sym}")).fetchone()
                    if prev is not None:
                        closed.append({**self._public(dict(prev)), "duplicate": True})
                        dup = True
                        continue
                    if sym not in prices:
                        continue
                    pos = self._position(uid, sym)
                    if pos["qty"] < 1e-12:
                        continue  # flat already (another request won the race)
                    row = self._close_locked(uid, sym, f"{cid}-x-{sym}", prices[sym][0], now)
                    closed.append({**self._public(row), "duplicate": False})
                self._db.commit()
            except BaseException:
                self._db.rollback()
                raise
        return {"closed": closed, "failed": failed, "duplicate": bool(closed) and all(c["duplicate"] for c in closed),
                "partialDuplicate": dup, "requested": list(wanted),
                "realized": sum(c["realized"] for c in closed if not c["duplicate"]),
                "fees": sum(c["fee"] for c in closed if not c["duplicate"])}

    def close_position(self, uid: str, symbol: Any, client_order_id: Any) -> dict:
        sym = self._norm_sym(symbol)
        if not sym:
            raise OrderError("UNKNOWN_SYMBOL", f"不支持的币种：{symbol}", 404, key="unknownSymbol", params={"s": str(symbol)[:20]})
        res = self._close_many(uid, client_order_id, [sym])
        if res["closed"]:
            return {**res, "order": res["closed"][0]}
        if res["failed"]:
            f = res["failed"][0]
            raise OrderError(f["code"], f["message"], 503, key=f.get("key"))
        raise OrderError("NO_POSITION", "没有这个币种的持仓（可能已经平仓）", 409, key="noPositionToClose")

    def close_all(self, uid: str, client_order_id: Any) -> dict:
        res = self._close_many(uid, client_order_id, None)
        if not res["closed"] and not res["failed"]:
            raise OrderError("NO_POSITION", "当前没有可平的持仓", 409, key="noPositionToClose")
        return res

    # ---- read model ----------------------------------------------------------
    @staticmethod
    def _public(o: dict) -> dict:
        return {
            "id": o["id"], "clientOrderId": o["client_id"], "ts": o["ts"], "symbol": o["symbol"], "side": o["side"],
            "type": o["type"], "qty": o["qty"], "limitPrice": o["limit_px"], "status": o["status"], "fillPrice": o["fill_px"],
            "fillTs": o["fill_ts"], "fee": o["fee"] or 0.0, "slippage": o["slippage"] or 0.0, "notional": o["notional"] or 0.0,
            "realized": o["realized"] or 0.0, "reason": o["reason"], "mode": "paper",
            "triggerPrice": o.get("trigger_px"), "ocoGroup": o.get("oco_group"),
        }

    def snapshot(self, uid: str, symbol: Optional[str] = None, *, limit: int = 50) -> dict:
        self.match_open(uid)
        with self._lock:
            acct = dict(self._account(uid))
            self._db.commit()
            res_cash, res_qty = self._reserved(uid)
            positions, value = [], 0.0
            for r in self._db.execute("SELECT * FROM positions WHERE user_id=? ORDER BY symbol", (uid,)):
                p = dict(r)
                got = None
                try:
                    got = self.price_fn(p["symbol"])
                except Exception:
                    pass
                last = float(got[0]) if got else p["avg_px"]
                mv = p["qty"] * last
                value += mv
                positions.append({
                    "symbol": p["symbol"], "qty": p["qty"], "avgPrice": p["avg_px"], "last": last, "value": mv,
                    "unrealized": (last - p["avg_px"]) * p["qty"], "realized": p["realized"], "fees": p["fees"],
                    "available": p["qty"] - res_qty.get(p["symbol"], 0.0),
                })
            q = "SELECT * FROM orders WHERE user_id=?"
            args: list[Any] = [uid]
            if symbol:
                q += " AND symbol=?"
                args.append(symbol)
            orders = [self._public(dict(r)) for r in self._db.execute(q + " ORDER BY ts DESC LIMIT ?", (*args, limit))]
            open_orders = [self._public(dict(r)) for r in self._db.execute("SELECT * FROM orders WHERE user_id=? AND status='open' ORDER BY ts DESC", (uid,))]
        equity = acct["cash"] + value
        lim = self.limits
        return {
            "mode": "paper",
            "cash": acct["cash"], "availableCash": acct["cash"] - res_cash, "startCash": acct["start_cash"],
            "equity": equity, "pnl": equity - acct["start_cash"], "pnlPct": equity / acct["start_cash"] - 1,
            "positions": positions, "openOrders": open_orders, "orders": orders,
            "limits": {"maxOrderUsdt": lim.max_order_usdt, "maxPositionUsdt": lim.max_position_usdt, "minOrderUsdt": lim.min_order_usdt,
                       "maxOpenOrders": lim.max_open_orders, "ordersPerMin": lim.orders_per_min},
            "costs": {s: self.costs(s) for s in self.symbols()},
            "live": {"enabled": False, "reason": "LIVE_API_LOCKED", "message": "实盘未开启"},
        }


_accounts: Optional[PaperAccounts] = None
_alock = threading.Lock()


def _market_price(sym: str) -> Optional[tuple[float, int]]:
    from app.marketdata.mainstream import get_service

    svc = get_service()
    if sym not in svc.cfg.all_symbols():  # 大盘 coin outside the pool: on-demand public 1m close
        from app.marketdata.extra import get_extra

        return get_extra().last_price(sym)
    for tf in ("1m", "1h"):
        try:
            rows = svc.chart_candles(sym, tf, limit=1)["candles"]
        except Exception:
            rows = []
        if rows:
            r = rows[-1]
            step = 60_000 if tf == "1m" else 3_600_000
            # bar open ts -> "price as of" = min(now, bar close)
            return float(r["close"]), min(int(time.time() * 1000), int(r["ts"]) + step)
    return None


def _market_candles(sym: str, since: int) -> list[dict]:
    from app.marketdata.mainstream import get_service

    svc = get_service()
    if sym not in svc.cfg.all_symbols():
        from app.marketdata.extra import get_extra

        return get_extra().bars_since(sym, since)
    ex = svc.exchange_for_read()
    if not ex:
        return []
    svc.chart_candles(sym, "1m", limit=5)  # refresh the tail (throttled)
    return svc.store.candles(ex, sym, "1m", since=since // 60_000 * 60_000, limit=2000)


def _paper_symbols() -> list[str]:
    from app.marketdata.extra import get_extra

    return get_extra().paper_symbols()


def held_symbols() -> set[str]:
    """Coins with an open paper position or open order (any user): always tradable/closable."""
    acc = _accounts
    if acc is None:
        return set()
    with acc._lock:
        rows = acc._db.execute(
            "SELECT symbol FROM positions WHERE qty > 1e-12 UNION SELECT symbol FROM orders WHERE status='open'"
        ).fetchall()
    return {str(r[0]) for r in rows}


def get_accounts() -> PaperAccounts:
    global _accounts
    with _alock:
        if _accounts is None:
            from app.marketdata.mainstream import get_service

            _accounts = PaperAccounts(price_fn=_market_price, candles_fn=_market_candles, symbols=_paper_symbols)  # pool + 大盘 top-100 coins + held; limits unchanged
        return _accounts


def reset_accounts(acc: Optional[PaperAccounts] = None) -> None:
    global _accounts
    with _alock:
        _accounts = acc
