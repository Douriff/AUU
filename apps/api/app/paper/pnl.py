"""Profit & loss view of a user's *manual* mainstream paper account (read-only).

Everything is computed here on the server from the existing paper tables (accounts / positions /
orders in mainstream_paper.sqlite); nothing is written and no table changes. The strategy paper
book and H2 live under /performance and are not mixed in.

* summary: total assets (cash + positions at last price), unrealized, realized (net of fees, as the
  ledger books it), today's P&L (vs. equity at 00:00 Beijing time), total return vs. start cash,
  one row per position. Every price carries its age; a price older than ``STALE_MS`` (or a missing
  one, valued at cost) is flagged so the page can say so.
* curve: equity over time rebuilt from the filled orders (cash/qty replay) valued with stored candle
  closes (1h for 7 days, 4h for 30 days, 1d for all), ending with the live equity point.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from app.paper.mainstream_account import PaperAccounts, get_accounts

BJ = timezone(timedelta(hours=8))
STALE_MS = 180_000  # same freshness bound the paper broker uses before refusing orders
RANGES = {"7d": ("1h", 7), "30d": ("4h", 30), "all": ("1d", None)}
TF_MS = {"1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}
MAX_POINTS = 400

# history_fn(symbol, tf, limit) -> [{"ts": bar open ms, "close": float}, ...] ascending
HistoryFn = Callable[[str, str, int], list[dict]]


def bj_midnight(ms: int) -> int:
    d = datetime.fromtimestamp(ms / 1000, BJ).replace(hour=0, minute=0, second=0, microsecond=0)
    return int(d.timestamp() * 1000)


def _market_history(sym: str, tf: str, limit: int) -> list[dict]:
    from app.marketdata.mainstream import get_service

    svc = get_service()
    try:
        if sym in svc.cfg.all_symbols():
            rows = svc.chart_candles(sym, tf, limit=limit)["candles"]
        else:
            from app.marketdata.extra import get_extra

            ex = get_extra()
            venue, _ = ex.resolve(sym)
            rows = ex.candles(venue, sym, tf, limit=limit)["candles"] if venue else []
    except Exception:
        rows = []
    return [{"ts": int(r["ts"]), "close": float(r["close"])} for r in rows if r.get("close") is not None]


class PnlView:
    def __init__(self, accounts: Optional[PaperAccounts] = None, *, history_fn: HistoryFn = _market_history,
                 now_ms: Optional[Callable[[], int]] = None):
        self._acc = accounts
        self.history_fn = history_fn
        self._now = now_ms

    @property
    def acc(self) -> PaperAccounts:
        return self._acc or get_accounts()

    def now(self) -> int:
        return self._now() if self._now else self.acc.now_ms()

    # ---- raw reads (no writes) ----------------------------------------------------------------
    def _rows(self, uid: str) -> tuple[Optional[dict], list[dict], list[dict]]:
        acc = self.acc
        with acc._lock:
            a = acc._db.execute("SELECT * FROM accounts WHERE user_id=?", (uid,)).fetchone()
            pos = [dict(r) for r in acc._db.execute("SELECT * FROM positions WHERE user_id=? ORDER BY symbol", (uid,))]
            fills = [dict(r) for r in acc._db.execute(
                "SELECT symbol, side, qty, fill_px, fill_ts, fee, notional FROM orders WHERE user_id=? AND status='filled' "
                "AND fill_ts IS NOT NULL ORDER BY fill_ts, ts", (uid,))]
        return (dict(a) if a else None), pos, fills

    def _price(self, sym: str) -> tuple[Optional[float], Optional[int]]:
        try:
            got = self.acc.price_fn(sym)
        except Exception:
            got = None
        if not got or not got[0]:
            return None, None
        return float(got[0]), int(got[1])

    # ---- summary ------------------------------------------------------------------------------
    def summary(self, uid: str) -> dict[str, Any]:
        acc = self.acc
        try:
            acc.match_open(uid)  # same lazy fill pass the trade page runs; keeps both views consistent
        except Exception:
            pass
        now = self.now()
        a, pos, fills = self._rows(uid)
        start = float(a["start_cash"]) if a else float(acc.limits.start_cash)
        cash = float(a["cash"]) if a else start
        created = int(a["created_ms"]) if a else now
        rows, value, unreal, realized, fees = [], 0.0, 0.0, 0.0, 0.0
        stale_any, missing_any, oldest = False, False, None
        for p in pos:
            realized += float(p["realized"])
            fees += float(p["fees"])
            qty = float(p["qty"])
            if qty <= 1e-12:
                continue
            avg = float(p["avg_px"])
            px, ts = self._price(p["symbol"])
            missing = px is None
            last = avg if missing else px
            age = None if ts is None else max(0, now - ts)
            stale = missing or (age is not None and age > STALE_MS)
            stale_any |= stale
            missing_any |= missing
            if ts is not None:
                oldest = ts if oldest is None else min(oldest, ts)
            mv = qty * last
            u = (last - avg) * qty
            value += mv
            unreal += u
            rows.append({"symbol": p["symbol"], "qty": qty, "avgPrice": avg, "last": last, "priceTs": ts, "priceAgeMs": age,
                         "stale": stale, "priceMissing": missing, "value": mv, "unrealized": u,
                         "unrealizedPct": (last / avg - 1) if avg > 0 else 0.0, "realized": float(p["realized"])})
        equity = cash + value
        rows.sort(key=lambda r: -r["value"])
        for r in rows:
            r["weight"] = r["value"] / equity if equity > 0 else 0.0
        midnight = bj_midnight(now)
        base = start if created >= midnight else self._equity_at(uid, midnight, start, created, fills)
        today = None if base is None else equity - base
        return {
            "mode": "paper", "scope": "manual", "currency": "USDT", "asOf": now,
            "equity": equity, "cash": cash, "positionsValue": value, "startCash": start,
            "unrealized": unreal, "realized": realized, "fees": fees,
            "today": today, "todayPct": (today / base) if (today is not None and base) else None, "todayBase": base,
            "totalPnl": equity - start, "totalReturn": equity / start - 1 if start else 0.0,
            "positions": rows, "stale": stale_any, "priceMissing": missing_any, "oldestPriceTs": oldest,
            "staleAfterMs": STALE_MS, "createdMs": created, "trades": len(fills),
        }

    # ---- history ------------------------------------------------------------------------------
    @staticmethod
    def _replay(fills: list[dict], start: float, until: int) -> tuple[float, dict[str, float]]:
        cash, qty = start, {}
        for f in fills:
            if int(f["fill_ts"]) > until:
                break
            n = float(f["notional"] or float(f["qty"]) * float(f["fill_px"]))
            fee = float(f["fee"] or 0.0)
            if f["side"] == "buy":
                cash -= n + fee
                qty[f["symbol"]] = qty.get(f["symbol"], 0.0) + float(f["qty"])
            else:
                cash += n - fee
                qty[f["symbol"]] = max(0.0, qty.get(f["symbol"], 0.0) - float(f["qty"]))
        return cash, {s: q for s, q in qty.items() if q > 1e-12}

    def _closes(self, syms: set[str], tf: str, limit: int) -> dict[str, list[dict]]:
        out = {}
        for s in sorted(syms):
            try:
                out[s] = sorted(self.history_fn(s, tf, limit), key=lambda r: r["ts"])
            except Exception:
                out[s] = []
        return out

    @staticmethod
    def _close_at(rows: list[dict], t: int, step: int) -> Optional[float]:
        """Close of the last bar that had closed by time t (bar open + step <= t)."""
        best = None
        for r in rows:
            if int(r["ts"]) + step <= t:
                best = r["close"]
            else:
                break
        return best

    def _avg_cost(self, fills: list[dict], sym: str, until: int) -> Optional[float]:
        q = c = 0.0
        for f in fills:
            if int(f["fill_ts"]) > until or f["symbol"] != sym:
                continue
            if f["side"] == "buy":
                c += float(f["qty"]) * float(f["fill_px"])
                q += float(f["qty"])
            elif q > 0:
                c *= max(0.0, 1 - float(f["qty"]) / q)
                q = max(0.0, q - float(f["qty"]))
        return c / q if q > 1e-12 else None

    def _value(self, t: int, cash: float, qty: dict[str, float], closes: dict[str, list[dict]], step: int,
               fills: list[dict]) -> tuple[float, bool]:
        v, approx = cash, False
        for s, q in qty.items():
            px = self._close_at(closes.get(s, []), t, step)
            if px is None:
                px = self._avg_cost(fills, s, t) or 0.0
                approx = True
            v += q * px
        return v, approx

    def _equity_at(self, uid: str, t: int, start: float, created: int, fills: list[dict]) -> Optional[float]:
        if t <= created:
            return start
        cash, qty = self._replay(fills, start, t)
        if not qty:
            return cash
        closes = self._closes(set(qty), "1h", 72)
        v, _ = self._value(t, cash, qty, closes, TF_MS["1h"], fills)
        return v

    def curve(self, uid: str, rng: str = "7d") -> dict[str, Any]:
        if rng not in RANGES:
            rng = "7d"
        tf, days = RANGES[rng]
        step = TF_MS[tf]
        now = self.now()
        a, pos, fills = self._rows(uid)
        start = float(a["start_cash"]) if a else float(self.acc.limits.start_cash)
        created = int(a["created_ms"]) if a else now
        lo = created if days is None else max(created, now - days * 86_400_000)
        first = (lo // step + 1) * step
        grid = list(range(first, now, step))
        if len(grid) > MAX_POINTS:
            k = -(-len(grid) // MAX_POINTS)
            grid = grid[::k]
        syms = {f["symbol"] for f in fills}
        need = int((now - lo) // step) + 3
        closes = self._closes(syms, tf, min(max(need, 2), 1000)) if syms else {}
        pts, approx_any = [{"ts": lo, "equity": self._value(lo, *self._replay(fills, start, lo), closes, step, fills)[0]
                           if lo > created else start}], False
        for t in grid:
            cash, qty = self._replay(fills, start, t)
            v, approx = self._value(t, cash, qty, closes, step, fills)
            approx_any |= approx
            pts.append({"ts": t, "equity": v, **({"approx": True} if approx else {})})
        live = self.summary(uid)
        pts.append({"ts": now, "equity": live["equity"], "live": True})
        base = pts[0]["equity"]
        return {"range": rng, "tf": tf, "points": pts, "start": base, "end": live["equity"],
                "change": live["equity"] - base, "changePct": (live["equity"] / base - 1) if base else 0.0,
                "startCash": start, "approx": approx_any, "stale": live["stale"], "createdMs": created}


_view: Optional[PnlView] = None


def get_pnl() -> PnlView:
    global _view
    if _view is None:
        _view = PnlView()
    return _view


def reset_pnl(v: Optional[PnlView] = None) -> None:
    global _view
    _view = v
