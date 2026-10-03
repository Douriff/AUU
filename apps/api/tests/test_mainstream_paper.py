"""Mainstream paper trading: per-user accounts, risk controls, auth, live stays locked."""
from __future__ import annotations

import os
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.paper.mainstream_account import Limits, OrderError, PaperAccounts, reset_accounts

NOW = 1_790_000_000_000


class Clock:
    def __init__(self, t=NOW):
        self.t = t

    def __call__(self):
        return self.t


class Market:
    def __init__(self, clock):
        self.px = {"BTC": 100_000.0, "ETH": 4_000.0}
        self.ts = {}
        self.bars: dict[str, list[dict]] = {"BTC": [], "ETH": []}
        self.clock = clock

    def price(self, sym):
        return (self.px[sym], self.ts.get(sym, self.clock())) if sym in self.px else None

    def candles(self, sym, since):
        return [b for b in self.bars.get(sym, []) if b["ts"] >= since // 60_000 * 60_000]


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = Clock()
        self.mkt = Market(self.clock)
        self.acc = PaperAccounts(
            Path(self.tmp.name) / "p.sqlite", price_fn=self.mkt.price, candles_fn=self.mkt.candles,
            symbols=lambda: ["BTC", "ETH", "SOL"], limits=Limits(), now_ms=self.clock,
        )
        self.n = 0

    def tearDown(self):
        self.acc.close()
        self.tmp.cleanup()

    def order(self, uid="u1", **kw):
        self.n += 1
        self.clock.t += 5_000  # outside the duplicate window unless a test says otherwise
        body = {"client_order_id": f"cid-{self.n:06d}", "symbol": "BTC", "side": "buy", "type": "market"}
        body.update(kw)
        return self.acc.place(uid, body)

    def code(self, **kw):
        with self.assertRaises(OrderError) as ctx:
            self.order(**kw)
        return ctx.exception.code


class AccountTests(_Base):
    def test_market_buy_then_sell_with_fees_slippage_and_pnl(self):
        o = self.order(notional=1000)
        self.assertEqual(o["status"], "filled")
        self.assertAlmostEqual(o["fillPrice"], 100_000 * 1.0001)  # BTC slippage 1bp
        self.assertAlmostEqual(o["fee"], o["notional"] * 0.0005)  # taker fee from the backtest CostModel
        snap = self.acc.snapshot("u1", "BTC")
        self.assertAlmostEqual(snap["cash"], 10_000 - o["notional"] - o["fee"])
        self.assertAlmostEqual(snap["positions"][0]["qty"], 0.01)
        self.mkt.px["BTC"] = 110_000.0
        snap = self.acc.snapshot("u1", "BTC")
        self.assertAlmostEqual(snap["positions"][0]["unrealized"], (110_000 - o["fillPrice"]) * 0.01)
        s = self.order(side="sell", qty=0.01)
        sell_px = 110_000 * 0.9999
        self.assertAlmostEqual(s["fillPrice"], sell_px)
        self.assertAlmostEqual(s["realized"], (sell_px - o["fillPrice"]) * 0.01 - s["fee"])
        snap = self.acc.snapshot("u1")
        self.assertEqual(snap["positions"][0]["qty"], 0.0)
        self.assertAlmostEqual(snap["equity"], 10_000 - o["fee"] + s["realized"])
        self.assertEqual(snap["live"], {"enabled": False, "reason": "LIVE_API_LOCKED", "message": "实盘未开启"})

    def test_risk_caps(self):
        self.assertEqual(self.code(notional=2500), "ORDER_CAP")  # per-order cap 2000
        self.assertEqual(self.code(notional=5), "ORDER_TOO_SMALL")
        for _ in range(2):
            self.order(notional=2000)
        self.assertEqual(self.code(notional=1500), "POSITION_CAP")  # 4000 held + 1500 > 5000
        self.assertEqual(self.code(symbol="ETH", side="sell", qty=0.1), "INSUFFICIENT_POSITION")  # no shorting
        self.assertEqual(self.code(symbol="DOGE", notional=50), "UNKNOWN_SYMBOL")
        self.assertEqual(self.code(type="stop", notional=50), "BAD_ORDER")
        self.assertEqual(self.code(notional=None), "BAD_QTY")

    def test_insufficient_cash(self):
        acc = PaperAccounts(Path(self.tmp.name) / "c.sqlite", price_fn=self.mkt.price, candles_fn=self.mkt.candles,
                            symbols=lambda: ["BTC"], limits=Limits(start_cash=500), now_ms=self.clock)
        with self.assertRaises(OrderError) as ctx:
            acc.place("u", {"client_order_id": "cash-000001", "symbol": "BTC", "side": "buy", "notional": 600})
        self.assertEqual(ctx.exception.code, "INSUFFICIENT_CASH")
        acc.close()

    def test_duplicate_submit_protection(self):
        first = self.acc.place("u1", {"client_order_id": "same-click-01", "symbol": "BTC", "side": "buy", "notional": 100})
        again = self.acc.place("u1", {"client_order_id": "same-click-01", "symbol": "BTC", "side": "buy", "notional": 100})
        self.assertEqual(again["id"], first["id"])
        self.assertTrue(again["duplicate"])
        self.assertEqual(len(self.acc.snapshot("u1")["orders"]), 1)  # replay did not fill twice
        with self.assertRaises(OrderError) as ctx:  # new id, identical order within 3 s
            self.acc.place("u1", {"client_order_id": "same-click-02", "symbol": "BTC", "side": "buy", "qty": first["qty"]})
        self.assertEqual(ctx.exception.code, "DUPLICATE_ORDER")
        self.assertEqual(self.code(client_order_id="x"), "BAD_CLIENT_ID")

    def test_rate_limit_and_stale_price(self):
        for i in range(20):
            self.acc.place("u2", {"client_order_id": f"rate-{i:04d}", "symbol": "ETH", "side": "buy", "notional": 10 + i})
        with self.assertRaises(OrderError) as ctx:
            self.acc.place("u2", {"client_order_id": "rate-9999", "symbol": "ETH", "side": "buy", "notional": 99})
        self.assertEqual(ctx.exception.code, "RATE_LIMIT")
        self.mkt.ts["BTC"] = NOW - 10 * 60_000
        self.assertEqual(self.code(notional=100), "STALE_PRICE")

    def test_limit_order_rests_reserves_fills_and_cancels(self):
        o = self.order(type="limit", limit_price=95_000, notional=950)
        self.assertEqual(o["status"], "open")
        snap = self.acc.snapshot("u1", "BTC")
        self.assertAlmostEqual(snap["availableCash"], 10_000 - 950 * 1.0002)
        self.mkt.bars["BTC"].append({"ts": o["ts"] // 60_000 * 60_000 + 60_000, "open": 99_000, "high": 99_500, "low": 94_900, "close": 96_000})
        snap = self.acc.snapshot("u1", "BTC")
        filled = snap["orders"][0]
        self.assertEqual(filled["status"], "filled")
        self.assertEqual(filled["fillPrice"], 95_000)
        self.assertAlmostEqual(filled["fee"], 950 * 0.0002)  # maker
        o2 = self.order(type="limit", limit_price=90_000, notional=900)
        self.assertEqual(self.acc.cancel("u1", o2["id"])["status"], "cancelled")
        with self.assertRaises(OrderError):
            self.acc.cancel("u1", o2["id"])
        with self.assertRaises(OrderError):
            self.acc.cancel("someone-else", o["id"])
        self.assertEqual(self.code(type="limit", limit_price=10, notional=100), "BAD_PRICE")

    def test_marketable_limit_fills_as_taker_not_worse_than_limit(self):
        o = self.order(type="limit", limit_price=101_000, qty=0.001)
        self.assertEqual(o["status"], "filled")
        self.assertAlmostEqual(o["fillPrice"], 100_000 * 1.0001)
        self.assertAlmostEqual(o["fee"], o["notional"] * 0.0005)

    def test_accounts_are_per_user(self):
        self.order(uid="alice", notional=1000)
        bob = self.acc.snapshot("bob")
        self.assertEqual(bob["positions"], [])
        self.assertEqual(bob["cash"], 10_000)
        self.assertEqual(bob["orders"], [])


    def test_paper_path_never_touches_exchange_order_apis(self):
        root = Path(__file__).resolve().parents[1] / "app"
        for f in (root / "paper" / "mainstream_account.py", root / "routes" / "mainstream_paper.py"):
            src = f.read_text(encoding="utf-8")
            for bad in ("ccxt", "create_order", "private_", "api_key", "apiKey", "secret"):
                self.assertNotIn(bad, src, f"{f.name} contains {bad}")


_ENV = ("AUU_AUTH", "AUU_ALLOW_SIGNUP", "AUU_INVITE_CODE", "AUU_ADMIN_USER", "AUU_USER_STORE", "AUU_USER_JOURNAL_DIR", "AUU_AUTH_RATE_MAX", "AUU_LEGACY_PUMP")
PW = "paperPass123"


class PaperApiTests(_Base):
    def setUp(self):
        super().setUp()
        from app.auth.accounts import reset_accounts as reset_users

        self._prev = {k: os.environ.get(k) for k in _ENV}
        root = Path(self.tmp.name)
        os.environ.update({"AUU_AUTH": "on", "AUU_ALLOW_SIGNUP": "on", "AUU_USER_STORE": str(root / "users.json"),
                           "AUU_USER_JOURNAL_DIR": str(root / "journals"), "AUU_AUTH_RATE_MAX": "1000", "AUU_LEGACY_PUMP": "off"})
        os.environ.pop("AUU_INVITE_CODE", None)
        os.environ.pop("AUU_ADMIN_USER", None)
        reset_users()
        reset_accounts(self.acc)
        self._sock = patch.object(socket.socket, "connect", side_effect=AssertionError("network access in test"))
        self._sock.start()
        from fastapi.testclient import TestClient
        from app.main import create_app

        self.TestClient = TestClient
        self.app = create_app(legacy=False)

    def tearDown(self):
        from app.auth.accounts import reset_accounts as reset_users

        self._sock.stop()
        reset_accounts(None)
        for k, v in self._prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        reset_users()
        super().tearDown()

    def user(self, name):
        c = self.TestClient(self.app)
        r = c.post("/api/v1/auth/register", json={"name": name, "password": PW, "password_confirm": PW})
        self.assertEqual(r.status_code, 200, r.text)
        return c

    BODY = {"client_order_id": "api-order-0001", "symbol": "BTC", "side": "buy", "type": "market", "notional": 100}

    def test_unauthenticated_orders_return_401(self):
        anon = self.TestClient(self.app)
        for method, path, body in [
            ("POST", "/api/v1/mainstream/paper/orders", self.BODY),
            ("GET", "/api/v1/mainstream/paper/account?symbol=BTC", None),
            ("POST", "/api/v1/mainstream/paper/orders/abc/cancel", {}),
        ]:
            with self.subTest(path=path):
                r = anon.request(method, path, json=body)
                self.assertEqual(r.status_code, 401, r.text)
                self.assertEqual(r.json()["error"]["code"], "AUTH_REQUIRED")
        bad = self.TestClient(self.app, cookies={"auu_session": "forged.9999999999.sig"})
        self.assertEqual(bad.post("/api/v1/mainstream/paper/orders", json=self.BODY).status_code, 401)
        self.assertEqual(self.acc.snapshot("anyone")["orders"], [])  # nothing was written

    def test_route_rechecks_session_even_without_gate(self):
        from starlette.requests import Request
        from app.routes import mainstream_paper

        req = Request({"type": "http", "method": "POST", "path": "/", "headers": [], "query_string": b""})
        self.assertIsNone(mainstream_paper._uid(req))

    def test_logged_in_user_trades_own_account(self):
        alice, bob = self.user("alice"), self.user("bob")
        r = alice.post("/api/v1/mainstream/paper/orders", json=self.BODY)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["data"]["status"], "filled")
        self.assertEqual(r.json()["data"]["mode"], "paper")
        a = alice.get("/api/v1/mainstream/paper/account?symbol=BTC").json()["data"]
        b = bob.get("/api/v1/mainstream/paper/account?symbol=BTC").json()["data"]
        self.assertEqual(len(a["orders"]), 1)
        self.assertEqual(b["orders"], [])
        self.assertEqual(b["cash"], 10_000)
        oid = r.json()["data"]["id"]
        self.assertEqual(bob.post(f"/api/v1/mainstream/paper/orders/{oid}/cancel").status_code, 404)

    def test_risk_errors_surface_as_http_codes(self):
        c = self.user("carol")
        r = c.post("/api/v1/mainstream/paper/orders", json={**self.BODY, "notional": 5000})
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (422, "ORDER_CAP"))
        r = c.post("/api/v1/mainstream/paper/orders", json={**self.BODY, "symbol": "ETH", "side": "sell", "qty": 0.1, "notional": None})
        self.assertEqual(r.json()["error"]["code"], "INSUFFICIENT_POSITION")

    def test_live_cannot_be_opened_from_the_web(self):
        c = self.user("dave")  # first user = admin
        r = c.post("/api/v1/mainstream/paper/orders", json={**self.BODY, "mode": "live"})
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (403, "LIVE_API_LOCKED"))
        r = c.post("/api/v1/mainstream/paper/orders", json={**self.BODY, "client_order_id": "api-order-0002", "venue": "binance"})
        self.assertEqual(r.status_code, 422)  # unknown fields refused
        for method, path, body in [
            ("PUT", "/api/v1/live/enabled", {"liveEnabled": True, "confirmed": True}),
            ("PUT", "/api/v1/live/arm", {"armed": True}),
        ]:
            r = c.request(method, path, json=body)
            self.assertEqual(r.status_code, 403, r.text)
            self.assertIn("LIVE_API_LOCKED", r.json()["error"]["reasons"])
        self.assertFalse(c.get("/api/v1/health").json()["data"]["liveEnabled"])
        self.assertEqual(self.acc.snapshot(c.get("/api/v1/auth/me").json()["data"]["user"]["id"])["orders"], [])

    def test_cross_site_order_refused(self):
        c = self.user("erin")
        r = c.post("/api/v1/mainstream/paper/orders", json=self.BODY, headers={"Origin": "https://evil.example"})
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (403, "CSRF_ORIGIN"))


if __name__ == "__main__":
    unittest.main()
