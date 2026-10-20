"""Paper USDT-M perpetual book (long + short), sharing the spot paper account's USDT cash.

Paper only: nothing here reaches an exchange. Lives in the same ``mainstream_paper.sqlite`` file but
uses its own, newly created tables (``perp_positions``, ``perp_orders``, ``perp_settings``) so the
existing spot tables/rows are never altered (additive migration).

Model (one-way mode: one net position per user+symbol):
- Leverage 1..max (``AUU_PAPER_PERP_MAX_LEVERAGE``, default 5, same default as the live futures cap).
- Margin mode ``cross`` or ``isolated``. Opening locks initial margin = notional / leverage from cash.
  Isolated: loss is capped at the position margin. Cross: free cash also backs the position, so the
  liquidation price is further away (computed with the account's free cash at snapshot time).
- Maintenance margin rate ``AUU_PAPER_PERP_MMR`` (default 0.5%). Liquidation price:
  long  p = (e*q - M) / (q*(1-mmr)) ; short p = (e*q + M) / (q*(1+mmr)), M = margin (+ free cash if cross).
  Liquidations are checked lazily (on every read/place) against 1m bar lows/highs since the last check
  and the last price; a liquidated position loses its margin (cross: down to the maintenance level of
  the equity backing it), recorded as a ``liquidation`` order.
- Fills: market only, last price +/- slippage (CostModel, same as spot) and the perp taker fee.
- Funding: settled every 8h (00/08/16 UTC) while the position is open, rate from ``funding_fn``
  (default ``AUU_PAPER_FUNDING_RATE`` = 0.01%). Positive rate: longs pay, shorts receive.
  Isolated: applied to the position margin; cross: applied to cash.
- Caps: margin per order <= max_order_usdt; position notional <= max_position_usdt * leverage.
Spot stays long-only by design (shorting is only offered through perps).
"""
from __future__ import annotations

import os
import uuid
from typing import Any, Callable, Optional

from app.paper.mainstream_account import OrderError

FUNDING_MS = 8 * 3_600_000

_PERP_SCHEMA = """
CREATE TABLE IF NOT EXISTS perp_settings (
  user_id TEXT NOT NULL, symbol TEXT NOT NULL, leverage INTEGER NOT NULL, margin_mode TEXT NOT NULL,
  PRIMARY KEY (user_id, symbol)
);
CREATE TABLE IF NOT EXISTS perp_positions (
  user_id TEXT NOT NULL, symbol TEXT NOT NULL, side TEXT NOT NULL, qty REAL NOT NULL, entry_px REAL NOT NULL,
  leverage INTEGER NOT NULL, margin_mode TEXT NOT NULL, margin REAL NOT NULL,
  realized REAL NOT NULL DEFAULT 0, fees REAL NOT NULL DEFAULT 0, funding REAL NOT NULL DEFAULT 0,
  opened_ts INTEGER NOT NULL, funding_ts INTEGER NOT NULL, checked_ts INTEGER NOT NULL,
  PRIMARY KEY (user_id, symbol)
);
CREATE TABLE IF NOT EXISTS perp_orders (
  id TEXT PRIMARY KEY, user_id TEXT NOT NULL, client_id TEXT NOT NULL, ts INTEGER NOT NULL,
  symbol TEXT NOT NULL, side TEXT NOT NULL, kind TEXT NOT NULL, qty REAL NOT NULL, fill_px REAL,
  leverage INTEGER, margin_mode TEXT, fee REAL DEFAULT 0, slippage REAL DEFAULT 0, notional REAL DEFAULT 0,
  realized REAL DEFAULT 0, status TEXT NOT NULL, reason TEXT,
  UNIQUE (user_id, client_id)
);
CREATE INDEX IF NOT EXISTS perp_orders_user_ts ON perp_orders (user_id, ts);
"""


def _f(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "") or default)
    except ValueError:
        return default


def liq_price(side: str, entry: float, qty: float, margin: float, mmr: float) -> float:
    if qty <= 0:
        return 0.0
    if side == "long":
        return max(0.0, (entry * qty - margin) / (qty * (1 - mmr)))
    return (entry * qty + margin) / (qty * (1 + mmr))


class PerpBook:
    def __init__(self, acc, funding_fn: Optional[Callable[[str], float]] = None):
        self.acc = acc
        self.max_leverage = int(_f("AUU_PAPER_PERP_MAX_LEVERAGE", 5))
        self.mmr = _f("AUU_PAPER_PERP_MMR", 0.005)
        rate = _f("AUU_PAPER_FUNDING_RATE", 0.0001)
        self.funding_fn = funding_fn or (lambda sym: rate)
        with acc._lock:
            acc._db.executescript(_PERP_SCHEMA)
            acc._db.commit()

    @property
    def _db(self):
        return self.acc._db

    # ---- settings ------------------------------------------------------------
    def check_leverage(self, lev: Any, mode: Any) -> tuple[int, str]:
        try:
            lv = int(lev)
            if float(lev) != lv:
                raise ValueError
        except (TypeError, ValueError):
            raise OrderError("LEVERAGE_TOO_HIGH", f"杠杆只能设置 1–{self.max_leverage} 倍", key="leverageRange", params={"v": self.max_leverage})
        if not (1 <= lv <= self.max_leverage):
            raise OrderError("LEVERAGE_TOO_HIGH", f"杠杆只能设置 1–{self.max_leverage} 倍", key="leverageRange", params={"v": self.max_leverage})
        if mode not in ("cross", "isolated"):
            raise OrderError("BAD_MARGIN_MODE", "保证金模式只能是 cross（全仓）或 isolated（逐仓）", key="badMarginMode")
        return lv, mode

    def _settings(self, uid: str, sym: str) -> tuple[int, str]:
        r = self._db.execute("SELECT leverage, margin_mode FROM perp_settings WHERE user_id=? AND symbol=?", (uid, sym)).fetchone()
        return (int(r[0]), str(r[1])) if r else (min(3, self.max_leverage), "cross")

    def set_leverage(self, uid: str, symbol: Any, leverage: Any, margin_mode: Any) -> dict:
        sym = self.acc._norm_sym(symbol)
        if sym not in self.acc.symbols():
            raise OrderError("UNKNOWN_SYMBOL", f"不支持的币种：{symbol}", 404, key="unknownSymbol", params={"s": str(symbol)[:20]})
        lv, mode = self.check_leverage(leverage, margin_mode)
        with self.acc._lock:
            pos = self._pos(uid, sym)
            if pos and (pos["leverage"] != lv or pos["margin_mode"] != mode):
                raise OrderError("POSITION_OPEN", "有持仓时不能修改杠杆或保证金模式，请先平仓", 409, key="perpSettingsLocked")
            self._db.execute("INSERT OR REPLACE INTO perp_settings VALUES (?,?,?,?)", (uid, sym, lv, mode))
            self._db.commit()
        return {"symbol": sym, "leverage": lv, "marginMode": mode}

    # ---- helpers -----------------------------------------------------------------
    def _pos(self, uid: str, sym: str) -> Optional[dict]:
        r = self._db.execute("SELECT * FROM perp_positions WHERE user_id=? AND symbol=?", (uid, sym)).fetchone()
        return dict(r) if r else None

    def _save(self, p: dict) -> None:
        cols = ("user_id", "symbol", "side", "qty", "entry_px", "leverage", "margin_mode", "margin", "realized", "fees",
                "funding", "opened_ts", "funding_ts", "checked_ts")
        self._db.execute(f"INSERT OR REPLACE INTO perp_positions ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                         tuple(p[c] for c in cols))

    def _cash(self, uid: str) -> float:
        return float(self.acc._account(uid)["cash"])

    def _add_cash(self, uid: str, delta: float) -> None:
        self._db.execute("UPDATE accounts SET cash=cash+? WHERE user_id=?", (delta, uid))

    def _free_cash(self, uid: str) -> float:
        res_cash, _ = self.acc._reserved(uid)
        return self._cash(uid) - res_cash

    def _record(self, uid: str, cid: str, sym: str, side: str, kind: str, qty: float, px: float, lev: int, mode: str,
                fee: float, slip: float, realized: float, now: int, reason: Optional[str] = None) -> dict:
        row = {"id": uuid.uuid4().hex[:16], "user_id": uid, "client_id": cid, "ts": now, "symbol": sym, "side": side,
               "kind": kind, "qty": qty, "fill_px": px, "leverage": lev, "margin_mode": mode, "fee": fee,
               "slippage": slip, "notional": qty * px, "realized": realized, "status": "filled", "reason": reason}
        self._db.execute(f"INSERT INTO perp_orders ({','.join(row)}) VALUES ({','.join('?' * len(row))})", tuple(row.values()))
        return row

    def _backing(self, uid: str, p: dict) -> float:
        """Margin backing the position for liquidation: isolated = its margin; cross = margin + free cash."""
        return p["margin"] + (max(0.0, self._free_cash(uid)) if p["margin_mode"] == "cross" else 0.0)

    # ---- lazy settlement: funding + liquidation --------------------------------
    def settle(self, uid: str) -> int:
        now = self.acc.now_ms()
        n = 0
        with self.acc._lock:
            for r in list(self._db.execute("SELECT * FROM perp_positions WHERE user_id=?", (uid,))):
                p = dict(r)
                self._funding(uid, p, now)
                if self._check_liq(uid, p, now):
                    n += 1
                else:
                    p["checked_ts"] = now
                    self._save(p)
            self._db.commit()
        return n

    def _funding(self, uid: str, p: dict, now: int) -> None:
        t = (p["funding_ts"] // FUNDING_MS + 1) * FUNDING_MS
        while t <= now:
            try:
                got = self.acc.price_fn(p["symbol"])
                mark = float(got[0]) if got else p["entry_px"]
            except Exception:
                mark = p["entry_px"]
            rate = float(self.funding_fn(p["symbol"]) or 0.0)
            pay = p["qty"] * mark * rate * (1 if p["side"] == "long" else -1)  # >0: position pays
            p["funding"] -= pay
            p["realized"] -= pay
            if p["margin_mode"] == "isolated":
                p["margin"] -= pay
            else:
                self._add_cash(uid, -pay)
            p["funding_ts"] = t
            t += FUNDING_MS
        self._save(p)

    def _check_liq(self, uid: str, p: dict, now: int) -> bool:
        lp = liq_price(p["side"], p["entry_px"], p["qty"], self._backing(uid, p), self.mmr)
        hit = None
        try:
            bars = self.acc.candles_fn(p["symbol"], p["checked_ts"] + 60_000) or []
        except Exception:
            bars = []
        for b in bars:
            if b["ts"] < p["checked_ts"] // 60_000 * 60_000 + 60_000:
                continue  # skip the bar the position was opened/last checked in
            if (p["side"] == "long" and b["low"] <= lp) or (p["side"] == "short" and b["high"] >= lp):
                hit = int(b["ts"])
                break
        if hit is None:
            try:
                got = self.acc.price_fn(p["symbol"])
            except Exception:
                got = None
            if got and ((p["side"] == "long" and got[0] <= lp) or (p["side"] == "short" and got[0] >= lp)):
                hit = now
        if hit is None:
            return False
        # Liquidated at lp: the backing equity falls to the maintenance level, which the "exchange" keeps.
        pnl = (lp - p["entry_px"]) * p["qty"] if p["side"] == "long" else (p["entry_px"] - lp) * p["qty"]
        loss = min(self._backing(uid, p), max(0.0, -pnl) + self.mmr * lp * p["qty"])
        cash_back = p["margin"] - loss  # may be negative for cross (paid from free cash)
        self._add_cash(uid, cash_back)
        self._record(uid, f"liq-{uuid.uuid4().hex[:12]}", p["symbol"], "sell" if p["side"] == "long" else "buy",
                     "liquidation", p["qty"], lp, p["leverage"], p["margin_mode"], 0.0, 0.0, -loss, hit, reason="LIQUIDATED")
        self._db.execute("DELETE FROM perp_positions WHERE user_id=? AND symbol=?", (uid, p["symbol"]))
        return True

    # ---- orders --------------------------------------------------------------------
    def _close_part(self, uid: str, p: dict, qty: float, fill: float, fee: float) -> float:
        frac = qty / p["qty"]
        pnl = (fill - p["entry_px"]) * qty if p["side"] == "long" else (p["entry_px"] - fill) * qty
        rel_margin = p["margin"] * frac
        self._add_cash(uid, rel_margin + pnl - fee)
        p["margin"] -= rel_margin
        p["qty"] -= qty
        p["realized"] += pnl - fee
        p["fees"] += fee
        if p["qty"] < 1e-12:
            self._db.execute("DELETE FROM perp_positions WHERE user_id=? AND symbol=?", (uid, p["symbol"]))
        else:
            self._save(p)
        return pnl - fee

    def place(self, uid: str, body: dict) -> dict:
        acc, lim = self.acc, self.acc.limits
        sym = acc._norm_sym(body.get("symbol"))
        if sym not in acc.symbols():
            raise OrderError("UNKNOWN_SYMBOL", f"不支持的币种：{body.get('symbol')}", 404, key="unknownSymbol", params={"s": str(body.get('symbol'))[:20]})
        side = body.get("side")
        if side not in ("buy", "sell"):
            raise OrderError("BAD_ORDER", "合约方向只能是 buy（开多/平空）或 sell（开空/平多）", key="badSideType")
        if body.get("type", "market") != "market":
            raise OrderError("BAD_ORDER", "纸面合约暂时只支持市价单", key="perpMarketOnly")
        reduce_only = bool(body.get("reduce_only"))
        cid = str(body.get("client_order_id") or "").strip()
        if not (8 <= len(cid) <= 64) or not all(ch.isalnum() or ch in "-_" for ch in cid):
            raise OrderError("BAD_CLIENT_ID", "缺少有效的 client_order_id（防重复提交）")
        self.settle(uid)
        now = acc.now_ms()
        with acc._lock:
            prev = self._db.execute("SELECT * FROM perp_orders WHERE user_id=? AND client_id=?", (uid, cid)).fetchone()
            if prev is not None:
                return {**self._public(dict(prev)), "duplicate": True}
            acc._account(uid)
            recent = self._db.execute("SELECT COUNT(*) FROM perp_orders WHERE user_id=? AND ts>? AND kind!='liquidation'", (uid, now - 60_000)).fetchone()[0]
            if recent >= lim.orders_per_min:
                raise OrderError("RATE_LIMIT", "下单太频繁，请稍后再试", 429)
            pos = self._pos(uid, sym)
            if pos:
                lev, mode = pos["leverage"], pos["margin_mode"]
            else:
                lev, mode = self._settings(uid, sym)
            if body.get("leverage") is not None or body.get("margin_mode") is not None:
                lev2, mode2 = self.check_leverage(body.get("leverage", lev), body.get("margin_mode", mode))
                if pos and (lev2, mode2) != (lev, mode):
                    raise OrderError("POSITION_OPEN", "有持仓时不能修改杠杆或保证金模式，请先平仓", 409, key="perpSettingsLocked")
                lev, mode = lev2, mode2
                self._db.execute("INSERT OR REPLACE INTO perp_settings VALUES (?,?,?,?)", (uid, sym, lev, mode))
            px, _ = acc._price(sym)
            try:
                if body.get("qty") not in (None, ""):
                    qty = float(body["qty"])
                elif body.get("notional") not in (None, ""):
                    qty = float(body["notional"]) / px
                elif body.get("margin") not in (None, ""):
                    qty = float(body["margin"]) * lev / px
                else:
                    raise OrderError("BAD_QTY", "请填写数量或金额", key="qtyRequired")
            except ValueError:
                raise OrderError("BAD_QTY", "数量或金额格式不正确", key="qtyFormat")
            qty = round(qty, 8)
            if not (qty > 0):
                raise OrderError("BAD_QTY", "数量必须大于 0", key="qtyPositive")
            slip = acc.slippage(sym)
            fill = px * (1 + slip) if side == "buy" else px * (1 - slip)
            fee_rate = acc.cost.taker * acc.cost.mult
            want = "long" if side == "buy" else "short"
            closing = 0.0
            if pos and pos["side"] != want:
                closing = min(qty, pos["qty"])
            opening = 0.0 if reduce_only else qty - closing
            if reduce_only and closing <= 0:
                raise OrderError("NO_POSITION", "只减仓：没有可平的反向持仓", 409, key="noPositionToClose")
            if opening > 0:
                notional = opening * px
                margin = notional / lev
                if notional < lim.min_order_usdt:
                    raise OrderError("ORDER_TOO_SMALL", f"单笔至少 {lim.min_order_usdt:g} USDT", key="orderMin", params={"v": f"{lim.min_order_usdt:g}"})
                if margin > lim.max_order_usdt + 1e-9:
                    raise OrderError("ORDER_CAP", f"单笔保证金上限 {lim.max_order_usdt:g} USDT", 422, key="perpOrderMax", params={"v": f"{lim.max_order_usdt:g}"})
                held = (pos["qty"] * px) if (pos and pos["side"] == want) else 0.0
                cap = lim.max_position_usdt * lev
                if held + notional > cap + 1e-9:
                    raise OrderError("POSITION_CAP", f"单币合约名义价值上限 {cap:g} USDT", 422, key="perpPosMax", params={"v": f"{cap:g}"})
                if margin + notional * fee_rate > self._free_cash(uid) + 1e-9:
                    raise OrderError("INSUFFICIENT_CASH", "可用保证金不足", 422, key="perpNoMargin")
            realized, fees = 0.0, 0.0
            if closing > 0:
                fee = closing * fill * fee_rate
                realized += self._close_part(uid, pos, closing, fill, fee)
                fees += fee
                pos = self._pos(uid, sym)
            if opening > 0:
                fee = opening * fill * fee_rate
                margin = opening * px / lev
                self._add_cash(uid, -margin - fee)
                fees += fee
                if pos:
                    tot = pos["qty"] + opening
                    pos["entry_px"] = (pos["entry_px"] * pos["qty"] + fill * opening) / tot
                    pos["qty"] = tot
                    pos["margin"] += margin
                    pos["fees"] += fee
                    pos["realized"] -= fee
                else:
                    pos = {"user_id": uid, "symbol": sym, "side": want, "qty": opening, "entry_px": fill, "leverage": lev,
                           "margin_mode": mode, "margin": margin, "realized": -fee, "fees": fee, "funding": 0.0,
                           "opened_ts": now, "funding_ts": now, "checked_ts": now}
                self._save(pos)
                realized -= fee
            kind = "close" if opening <= 0 else ("open" if closing <= 0 else "flip")
            row = self._record(uid, cid, sym, side, kind, closing + opening, fill, lev, mode, fees, slip * (closing + opening) * px, realized, now)
            self._db.commit()
        return {**self._public(row), "duplicate": False}

    def close_position(self, uid: str, symbol: Any, client_order_id: Any) -> dict:
        sym = self.acc._norm_sym(symbol)
        self.settle(uid)
        with self.acc._lock:
            cid = self.acc._close_cid(client_order_id) + "-px"
            prev = self._db.execute("SELECT * FROM perp_orders WHERE user_id=? AND client_id=?", (uid, cid)).fetchone()
            if prev is not None:
                return {"order": {**self._public(dict(prev)), "duplicate": True}, "duplicate": True}
            pos = self._pos(uid, sym)
        if not pos:
            raise OrderError("NO_POSITION", "没有这个币种的合约持仓（可能已经平仓或被强平）", 409, key="noPositionToClose")
        o = self.place(uid, {"client_order_id": cid, "symbol": sym, "side": "sell" if pos["side"] == "long" else "buy",
                             "qty": pos["qty"], "reduce_only": True})
        return {"order": o, "duplicate": False}

    def close_all(self, uid: str, client_order_id: Any) -> list[dict]:
        out = []
        base = self.acc._close_cid(client_order_id)
        for r in list(self._db.execute("SELECT symbol FROM perp_positions WHERE user_id=? ORDER BY symbol", (uid,))):
            try:
                out.append(self.close_position(uid, r[0], f"{base}-{r[0]}"[:64])["order"])
            except OrderError:
                pass
        return out

    # ---- read model ------------------------------------------------------------
    @staticmethod
    def _public(o: dict) -> dict:
        return {"id": o["id"], "clientOrderId": o["client_id"], "ts": o["ts"], "symbol": o["symbol"], "side": o["side"],
                "kind": o["kind"], "qty": o["qty"], "fillPrice": o["fill_px"], "leverage": o["leverage"],
                "marginMode": o["margin_mode"], "fee": o["fee"], "slippage": o["slippage"], "notional": o["notional"],
                "realized": o["realized"], "status": o["status"], "reason": o["reason"], "market": "perp"}

    def snapshot(self, uid: str, symbol: Optional[str] = None, limit: int = 50) -> dict:
        """Caller should have run settle(uid). Returns perp positions/orders and their equity contribution."""
        positions, value = [], 0.0
        free = self._free_cash(uid)
        for r in self._db.execute("SELECT * FROM perp_positions WHERE user_id=? ORDER BY symbol", (uid,)):
            p = dict(r)
            try:
                got = self.acc.price_fn(p["symbol"])
            except Exception:
                got = None
            last = float(got[0]) if got else p["entry_px"]
            upnl = (last - p["entry_px"]) * p["qty"] if p["side"] == "long" else (p["entry_px"] - last) * p["qty"]
            backing = p["margin"] + (max(0.0, free) if p["margin_mode"] == "cross" else 0.0)
            value += p["margin"] + upnl
            positions.append({
                "symbol": p["symbol"], "side": p["side"], "qty": p["qty"], "entryPrice": p["entry_px"], "last": last,
                "notional": p["qty"] * last, "leverage": p["leverage"], "marginMode": p["margin_mode"], "margin": p["margin"],
                "unrealized": upnl, "roe": upnl / p["margin"] if p["margin"] > 0 else 0.0,
                "liqPrice": liq_price(p["side"], p["entry_px"], p["qty"], backing, self.mmr),
                "realized": p["realized"], "fees": p["fees"], "funding": p["funding"],
                "nextFundingTs": (p["funding_ts"] // FUNDING_MS + 1) * FUNDING_MS,
            })
        q, args = "SELECT * FROM perp_orders WHERE user_id=?", [uid]
        if symbol:
            q += " AND symbol=?"
            args.append(symbol)
        orders = [self._public(dict(r)) for r in self._db.execute(q + " ORDER BY ts DESC LIMIT ?", (*args, limit))]
        settings = {}
        if symbol:
            lv, md = self._settings(uid, symbol)
            settings = {"leverage": lv, "marginMode": md}
        return {"positions": positions, "orders": orders, "value": value, "settings": settings,
                "maxLeverage": self.max_leverage, "mmr": self.mmr, "fundingRate": float(self.funding_fn(symbol) if symbol else self.funding_fn("")),
                "takerFee": self.acc.cost.taker * self.acc.cost.mult}
