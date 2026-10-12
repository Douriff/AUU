"""Points trading: same market-data fills as paper, capital = available (unfrozen) points.

Separate sqlite (``points_trading.sqlite``) for positions/orders. Every fill credits/debits the
points ledger (``points_trade_buy`` / ``points_trade_sell`` / ``points_trade_fee``). Paper P&L and
the paper ledger are never touched. Manual market/limit/TP-SL only; no auto strategy.

Flag: ``AUU_POINTS_TRADING`` (default off). ``AUU_POINTS_TRADING_ADMIN_ONLY=on`` soft-launches to
admins. Rate defaults to ``AUU_TRC20_RATE`` (100 points per 1 USDT).
"""
from __future__ import annotations

import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

from app.backtest.costs import CostModel
from app.data_paths import data_dir
from app.paper.mainstream_account import (
    MAKER_FEE,
    TRIGGER_TYPES,
    CandlesFn,
    Limits,
    OrderError,
    PaperAccounts,
    PriceFn,
    _market_candles,
    _market_price,
    _paper_symbols,
)
from app.points.ledger import PointsError, get_points_ledger

def _on(name: str, default: str = "off") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "on", "yes"}


def _int(name: str, default: int, lo: int, hi: int) -> int:
    try:
        v = int(str(os.getenv(name, "") or default).strip())
    except ValueError:
        v = default
    return max(lo, min(hi, v))


def rate() -> int:
    return _int("AUU_TRC20_RATE", 100, 1, 100_000)


def enabled() -> bool:
    return _on("AUU_POINTS_TRADING", "off")


def admin_only() -> bool:
    return _on("AUU_POINTS_TRADING_ADMIN_ONLY", "off")


def enabled_for(is_admin: bool = False) -> bool:
    return enabled() and (is_admin or not admin_only())


def public_config(is_admin: bool = False) -> dict[str, Any]:
    return {
        "enabled": enabled_for(is_admin),
        "adminOnly": admin_only(),
        "rate": rate(),
        "currency": "points",
        "quote": "USDT",
        "manualOnly": True,
        "autoStrategy": False,
    }


def usdt_to_points(usdt: float, r: Optional[int] = None) -> int:
    """Round half away from zero so a buy never under-charges."""
    rr = rate() if r is None else r
    if usdt >= 0:
        return int(usdt * rr + 0.5)
    return -int((-usdt) * rr + 0.5)


def points_to_usdt(points: int, r: Optional[int] = None) -> float:
    rr = rate() if r is None else r
    return float(points) / float(rr)


class PointsTradingAccounts(PaperAccounts):
    """PaperAccounts twin: cash column holds POINTS (whole numbers), fills write the points ledger."""

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
        points_rate: Optional[int] = None,
    ):
        lim = limits or Limits(
            start_cash=0.0,  # capital comes from the points wallet, not a paper grant
            max_order_usdt=_env_f("AUU_POINTS_TRADE_MAX_ORDER_USDT", 2_000.0),
            max_position_usdt=_env_f("AUU_POINTS_TRADE_MAX_POSITION_USDT", 5_000.0),
            min_order_usdt=_env_f("AUU_POINTS_TRADE_MIN_ORDER_USDT", 10.0),
            max_open_orders=int(_env_f("AUU_POINTS_TRADE_MAX_OPEN_ORDERS", 20)),
            orders_per_min=int(_env_f("AUU_POINTS_TRADE_ORDERS_PER_MIN", 20)),
        )
        super().__init__(
            path=path or (data_dir() / "points_trading.sqlite"),
            price_fn=price_fn,
            candles_fn=candles_fn,
            symbols=symbols,
            limits=lim,
            cost=cost,
            now_ms=now_ms,
        )
        self.points_rate = int(points_rate if points_rate is not None else rate())

    def _fp(self, **fields: Any) -> str:
        import hashlib
        import json

        return hashlib.sha256(json.dumps(fields, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def available_points(self, uid: str) -> int:
        """Spendable points = ledger balance minus points reserved on open buy orders."""
        bal = get_points_ledger().balance(uid)
        with self._lock:
            reserved_usdt = 0.0
            for r in self._db.execute(
                "SELECT reserved FROM orders WHERE user_id=? AND status='open' AND side='buy'", (uid,)
            ):
                reserved_usdt += float(r["reserved"] or 0)
        return max(0, bal - usdt_to_points(reserved_usdt, self.points_rate))

    def _sync_cash_from_points(self, uid: str) -> None:
        """accounts.cash mirrors spendable+reserved points so parent cash checks see the wallet."""
        bal = float(get_points_ledger().balance(uid))
        acct = self._account(uid)
        if abs(float(acct["cash"]) - bal) > 1e-9 or float(acct["start_cash"]) != 0.0:
            self._db.execute("UPDATE accounts SET cash=?, start_cash=0 WHERE user_id=?", (bal, uid))

    def _apply_fill(self, uid: str, order: dict, px: float, fee_rate: float, slip: float, ts: int) -> None:
        sym, side, qty = order["symbol"], order["side"], order["qty"]
        notional = qty * px
        fee = notional * fee_rate
        pos = self._position(uid, sym)
        realized = 0.0
        buy_pts = fee_pts = sell_pts = 0
        if side == "buy":
            new_qty = pos["qty"] + qty
            pos["avg_px"] = (pos["avg_px"] * pos["qty"] + notional) / new_qty
            pos["qty"] = new_qty
            buy_pts = usdt_to_points(notional, self.points_rate)
            fee_pts = usdt_to_points(fee, self.points_rate)
            if buy_pts + fee_pts > get_points_ledger().balance(uid):
                raise OrderError("INSUFFICIENT_POINTS", "可用积分不足", 422)
            pos["realized"] += -points_to_usdt(fee_pts, self.points_rate)
            pos["fees"] += fee
        else:
            sell_pts = usdt_to_points(notional, self.points_rate)
            fee_pts = usdt_to_points(fee, self.points_rate)
            realized = (px - pos["avg_px"]) * qty - fee
            pos["qty"] = max(0.0, pos["qty"] - qty)
            if pos["qty"] < 1e-12:
                pos["qty"], pos["avg_px"] = 0.0, 0.0
            pos["realized"] += realized
            pos["fees"] += fee
        self._save_position(pos)
        self._db.execute(
            "UPDATE orders SET status='filled', fill_px=?, fill_ts=?, fee=?, slippage=?, notional=?, reserved=0, realized=? WHERE id=?",
            (px, ts, fee, slip * qty * (order.get("ref_px") or px), notional, realized, order["id"]),
        )
        # Ledger last (idempotent on order id). Paper rows above stay in the open transaction until place() commits.
        try:
            if side == "buy":
                get_points_ledger().append(
                    user_id=uid, kind="points_trade_buy", amount=-buy_pts, reason=f"points trade buy {sym}",
                    operator=f"user:{uid}", idem_key=f"points_trade_buy:{order['id']}",
                    fingerprint=self._fp(a="points_trade_buy", o=order["id"], u=uid, p=buy_pts, sym=sym),
                    ref=f"order:{order['id']}", provider="points_trading")
                if fee_pts > 0:
                    get_points_ledger().append(
                        user_id=uid, kind="points_trade_fee", amount=-fee_pts, reason=f"points trade fee {sym} buy",
                        operator=f"user:{uid}", idem_key=f"points_trade_fee:{order['id']}",
                        fingerprint=self._fp(a="points_trade_fee", o=order["id"], u=uid, p=fee_pts, side="buy"),
                        ref=f"order:{order['id']}", provider="points_trading")
            else:
                get_points_ledger().append(
                    user_id=uid, kind="points_trade_sell", amount=sell_pts, reason=f"points trade sell {sym}",
                    operator=f"user:{uid}", idem_key=f"points_trade_sell:{order['id']}",
                    fingerprint=self._fp(a="points_trade_sell", o=order["id"], u=uid, p=sell_pts, sym=sym),
                    ref=f"order:{order['id']}", provider="points_trading")
                if fee_pts > 0:
                    get_points_ledger().append(
                        user_id=uid, kind="points_trade_fee", amount=-fee_pts, reason=f"points trade fee {sym} sell",
                        operator=f"user:{uid}", idem_key=f"points_trade_fee:{order['id']}",
                        fingerprint=self._fp(a="points_trade_fee", o=order["id"], u=uid, p=fee_pts, side="sell"),
                        ref=f"order:{order['id']}", provider="points_trading")
        except PointsError as exc:
            if str(exc) == "POINTS_INSUFFICIENT":
                raise OrderError("INSUFFICIENT_POINTS", "可用积分不足", 422) from exc
            raise OrderError("POINTS_LEDGER", str(exc), 500) from exc
        cash = float(get_points_ledger().balance(uid))
        self._db.execute("UPDATE accounts SET cash=? WHERE user_id=?", (cash, uid))

    def place(self, uid: str, body: dict) -> dict:
        if not enabled():
            raise OrderError("POINTS_TRADING_OFF", "积分交易暂未开放", 403)
        with self._lock:
            self._account(uid)
            self._sync_cash_from_points(uid)
            self._db.commit()
        # Convert reserved for limit buys into points (parent stores USDT reserved)
        # Monkey-patch: temporarily express cash in USDT-equivalent for parent checks, then restore.
        # Instead, override the insufficient-cash path by pre-checking in points.
        lim = self.limits
        sym = "".join(ch for ch in str(body.get("symbol", "")).split("/")[0].upper() if ch.isalnum())
        side, otype = body.get("side"), body.get("type", "market")
        if otype in TRIGGER_TYPES or otype == "oco":
            # TP/SL do not spend cash up front; reuse parent after sync
            with self._lock:
                self._sync_cash_from_points(uid)
            out = super().place(uid, body)
            out["mode"] = "points"
            return out
        if side not in ("buy", "sell") or otype not in ("market", "limit"):
            raise OrderError("BAD_ORDER", "side 必须是 buy/sell，type 必须是 market/limit/take_profit/stop_loss", key="badSideType")
        # Estimate points need for buys before delegating
        if side == "buy":
            try:
                px, _ = self._price(sym) if sym in self.symbols() else (0.0, 0)
            except OrderError:
                px = 0.0
            ref = float(body.get("limit_price") or 0) or px
            qty = body.get("qty")
            notional_in = body.get("notional")
            try:
                if qty not in (None, ""):
                    q = float(qty)
                    notion = q * ref if ref else 0.0
                elif notional_in not in (None, ""):
                    notion = float(notional_in)
                else:
                    notion = 0.0
            except (TypeError, ValueError):
                notion = 0.0
            fee_rate = self.cost.taker * self.cost.mult if otype == "market" else MAKER_FEE
            need = usdt_to_points(notion * (1 + fee_rate), self.points_rate) if notion else 0
            if need and need > self.available_points(uid):
                raise OrderError("INSUFFICIENT_POINTS", "可用积分不足", 422)
        # Parent cash is in "points units stored as float"; convert checks by expressing USDT limits
        # against cash/rate. Sync cash = points balance, then scale parent's cash check by treating
        # max costs in USDT while cash is points — mismatch!
        #
        # Fix: temporarily set cash to points_to_usdt(balance) so parent USDT math works, then
        # _apply_fill writes ledger and resets cash to points balance.
        with self._lock:
            bal = get_points_ledger().balance(uid)
            self._account(uid)
            self._db.execute(
                "UPDATE accounts SET cash=?, start_cash=0 WHERE user_id=?",
                (points_to_usdt(bal, self.points_rate), uid),
            )
            # Re-express open buy reserved from points-back to USDT for parent
            for r in self._db.execute(
                "SELECT id, reserved FROM orders WHERE user_id=? AND status='open' AND side='buy'", (uid,)
            ):
                # reserved already stored in USDT by parent place(); leave as-is
                pass
            self._db.commit()
        try:
            out = super().place(uid, body)
        except OrderError as e:
            if e.code == "INSUFFICIENT_CASH":
                raise OrderError("INSUFFICIENT_POINTS", "可用积分不足", 422) from e
            raise
        finally:
            with self._lock:
                self._db.execute(
                    "UPDATE accounts SET cash=? WHERE user_id=?",
                    (float(get_points_ledger().balance(uid)), uid),
                )
                self._db.commit()
        out["mode"] = "points"
        out["pointsBalance"] = get_points_ledger().balance(uid)
        return out

    def cancel(self, uid: str, order_id: str) -> dict:
        out = super().cancel(uid, order_id)
        out["mode"] = "points"
        return out

    @staticmethod
    def _public(o: dict) -> dict:
        pub = PaperAccounts._public(o)
        pub["mode"] = "points"
        return pub

    def snapshot(self, uid: str, symbol: Optional[str] = None, *, limit: int = 50) -> dict:
        self.match_open(uid)
        with self._lock:
            self._account(uid)
            bal = get_points_ledger().balance(uid)
            self._db.execute("UPDATE accounts SET cash=?, start_cash=0 WHERE user_id=?", (float(bal), uid))
            self._db.commit()
            res_cash_usdt, res_qty = self._reserved(uid)
            # reserved is USDT; convert for display
            res_pts = usdt_to_points(res_cash_usdt, self.points_rate)
            positions, value_usdt = [], 0.0
            for r in self._db.execute("SELECT * FROM positions WHERE user_id=? ORDER BY symbol", (uid,)):
                p = dict(r)
                got = None
                try:
                    got = self.price_fn(p["symbol"])
                except Exception:
                    pass
                last = float(got[0]) if got else p["avg_px"]
                mv = p["qty"] * last
                value_usdt += mv
                positions.append({
                    "symbol": p["symbol"], "qty": p["qty"], "avgPrice": p["avg_px"], "last": last,
                    "value": mv, "valuePoints": usdt_to_points(mv, self.points_rate),
                    "unrealized": (last - p["avg_px"]) * p["qty"],
                    "unrealizedPoints": usdt_to_points((last - p["avg_px"]) * p["qty"], self.points_rate),
                    "realized": p["realized"],
                    "realizedPoints": usdt_to_points(p["realized"], self.points_rate),
                    "fees": p["fees"], "available": p["qty"] - res_qty.get(p["symbol"], 0.0),
                })
            q = "SELECT * FROM orders WHERE user_id=?"
            args: list[Any] = [uid]
            if symbol:
                q += " AND symbol=?"
                args.append(symbol)
            orders = [self._public(dict(r)) for r in self._db.execute(q + " ORDER BY ts DESC LIMIT ?", (*args, limit))]
            open_orders = [
                self._public(dict(r))
                for r in self._db.execute(
                    "SELECT * FROM orders WHERE user_id=? AND status='open' ORDER BY ts DESC", (uid,)
                )
            ]
        value_pts = usdt_to_points(value_usdt, self.points_rate)
        equity_pts = bal + value_pts
        # starting equity proxy: current equity - unrealized - realized on positions
        realized_usdt = sum(float(p["realized"]) for p in positions)
        lim = self.limits
        return {
            "mode": "points",
            "rate": self.points_rate,
            "cash": bal,
            "availableCash": max(0, bal - res_pts),
            "availablePoints": max(0, bal - res_pts),
            "pointsBalance": bal,
            "startCash": 0,
            "equity": equity_pts,
            "equityUsdt": points_to_usdt(equity_pts, self.points_rate),
            "pnl": usdt_to_points(realized_usdt, self.points_rate) + usdt_to_points(
                sum((p["last"] - p["avgPrice"]) * p["qty"] for p in positions), self.points_rate
            ),
            "pnlUsdt": realized_usdt + sum((p["last"] - p["avgPrice"]) * p["qty"] for p in positions),
            "positions": positions,
            "openOrders": open_orders,
            "orders": orders,
            "limits": {
                "maxOrderUsdt": lim.max_order_usdt,
                "maxPositionUsdt": lim.max_position_usdt,
                "minOrderUsdt": lim.min_order_usdt,
                "maxOpenOrders": lim.max_open_orders,
                "ordersPerMin": lim.orders_per_min,
            },
            "costs": {s: self.costs(s) for s in self.symbols()},
            "live": {"enabled": False, "reason": "LIVE_API_LOCKED", "message": "实盘未开启"},
        }


def _env_f(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "") or default)
    except ValueError:
        return default


_accounts: Optional[PointsTradingAccounts] = None
_alock = threading.Lock()


def get_points_accounts() -> PointsTradingAccounts:
    global _accounts
    with _alock:
        if _accounts is None:
            _accounts = PointsTradingAccounts(
                price_fn=_market_price, candles_fn=_market_candles, symbols=_paper_symbols
            )
        return _accounts


def reset_points_accounts(acc: Optional[PointsTradingAccounts] = None) -> None:
    global _accounts
    with _alock:
        _accounts = acc


def admin_pnl_board(limit: int = 100) -> list[dict[str, Any]]:
    """Per-user points-trading P&L snapshot for the admin console."""
    from app.auth.accounts import list_users

    acc = get_points_accounts()
    out = []
    for u in list_users()[: max(1, min(int(limit), 500))]:
        uid = u["id"]
        try:
            snap = acc.snapshot(uid, limit=5)
        except Exception:
            continue
        if not snap["positions"] and not snap["orders"] and snap["pointsBalance"] == get_points_ledger().balance(uid):
            # skip users who never touched points trading (no orders ever)
            with acc._lock:
                n = acc._db.execute("SELECT COUNT(*) FROM orders WHERE user_id=?", (uid,)).fetchone()[0]
            if n == 0:
                continue
        out.append({
            "userId": uid,
            "name": u.get("name"),
            "displayName": u.get("display_name") or u.get("name"),
            "pointsBalance": snap["pointsBalance"],
            "equityPoints": snap["equity"],
            "pnlPoints": snap["pnl"],
            "pnlUsdt": snap["pnlUsdt"],
            "positions": len([p for p in snap["positions"] if p["qty"] > 1e-12]),
        })
    return out
