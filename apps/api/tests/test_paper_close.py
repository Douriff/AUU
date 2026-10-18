"""手动纸面账户「平仓 / 全部平仓」: correctness (price, fee, slippage, P&L), idempotency, own-account only,
concurrency, no side effects on strategy / shadow books. Paper only: no exchange client is involved."""
from __future__ import annotations

import hashlib
import os
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from app.paper.mainstream_account import Limits, OrderError, PaperAccounts, reset_accounts
from app.paper.pnl import PnlView
from tests.test_mainstream_paper import NOW, Clock, Market


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = Clock()
        self.mkt = Market(self.clock)
        self.path = Path(self.tmp.name) / "p.sqlite"
        self.acc = PaperAccounts(self.path, price_fn=self.mkt.price, candles_fn=self.mkt.candles,
                                 symbols=lambda: ["BTC", "ETH", "SOL"], limits=Limits(), now_ms=self.clock)
        self.n = 0

    def tearDown(self):
        self.acc.close()
        self.tmp.cleanup()

    def buy(self, sym="BTC", usdt=1000, uid="u1"):
        self.n += 1
        self.clock.t += 5_000
        return self.acc.place(uid, {"client_order_id": f"buy-order-{self.n:06d}", "symbol": sym, "side": "buy", "type": "market", "notional": usdt})

    def sell_limit(self, sym, qty, px, uid="u1"):
        self.n += 1
        self.clock.t += 5_000
        return self.acc.place(uid, {"client_order_id": f"sell-order-{self.n:06d}", "symbol": sym, "side": "sell", "type": "limit", "qty": qty, "limit_price": px})

    def pos(self, sym="BTC", uid="u1"):
        return next((p for p in self.acc.snapshot(uid)["positions"] if p["symbol"] == sym and p["qty"] > 1e-12), None)


class CloseCorrectnessTests(_Base):
    def test_close_fills_at_header_price_with_fee_slippage_and_books_pnl(self):
        b = self.buy("BTC", 1000)
        qty = self.pos()["qty"]
        self.mkt.px["BTC"] = 110_000.0
        cash0 = self.acc.snapshot("u1")["cash"]
        r = self.acc.close_position("u1", "BTC", "close-cid-0001")
        o = r["order"]
        self.assertEqual((o["status"], o["side"], o["type"], o["reason"]), ("filled", "sell", "market", "CLOSE"))
        self.assertAlmostEqual(o["qty"], qty)
        self.assertAlmostEqual(o["fillPrice"], 110_000 * (1 - 0.0001))  # BTC 1bp slippage, same as the paper market sell
        self.assertAlmostEqual(o["fee"], o["notional"] * 0.0005)  # taker fee from the shared CostModel
        self.assertFalse(o["duplicate"])
        snap = self.acc.snapshot("u1")
        self.assertAlmostEqual(snap["cash"], cash0 + o["notional"] - o["fee"])
        self.assertIsNone(self.pos())
        want = (o["fillPrice"] - b["fillPrice"]) * qty - o["fee"]
        self.assertAlmostEqual(o["realized"], want)
        self.assertAlmostEqual(r["realized"], want)
        # same figures the P&L page shows, from the same price source
        view = PnlView(self.acc, history_fn=lambda *a: [], now_ms=self.clock)
        s = view.summary("u1")
        self.assertEqual(s["positions"], [])
        self.assertAlmostEqual(s["realized"], want - b["fee"])
        self.assertAlmostEqual(s["equity"] - 10_000, s["realized"], places=6)
        self.assertIn(o["id"], [x["id"] for x in snap["orders"]])  # ledger shows the close

    def test_same_result_as_a_manual_market_sell(self):
        self.buy("BTC", 1000, "alice")
        self.buy("BTC", 1000, "bob")
        self.mkt.px["BTC"] = 101_234.5
        qty = self.pos("BTC", "alice")["qty"]
        self.acc.close_position("alice", "BTC", "close-cid-alice")
        self.clock.t += 5_000
        self.acc.place("bob", {"client_order_id": "manual-sell-001", "symbol": "BTC", "side": "sell", "type": "market", "qty": qty})
        a, b = self.acc.snapshot("alice"), self.acc.snapshot("bob")
        self.assertAlmostEqual(a["cash"], b["cash"])
        self.assertAlmostEqual(a["equity"], b["equity"])

    def test_close_ignores_order_size_caps_and_dust_minimum(self):
        for _ in range(2):
            self.buy("BTC", 1900)  # 3,800 > max order 2,000: only possible in pieces, but closing must work in one go
        big = self.pos()
        self.assertGreater(big["value"], self.acc.limits.max_order_usdt)
        r = self.acc.close_position("u1", "BTC", "close-cid-big0")
        self.assertEqual(r["order"]["status"], "filled")
        self.buy("ETH", 20)
        self.mkt.px["ETH"] = 2.0  # dust: far below the 10 USDT minimum
        self.assertEqual(self.acc.close_position("u1", "ETH", "close-cid-dust")["order"]["status"], "filled")

    def test_resting_sell_side_orders_are_cancelled_buy_orders_kept(self):
        self.buy("BTC", 1000)
        q = self.pos()["qty"]
        tp = self.acc.place("u1", {"client_order_id": "tpsl-oco-0001", "symbol": "BTC", "side": "sell", "type": "oco",
                                   "qty": q, "take_profit_price": 130_000, "stop_loss_price": 80_000})
        self.assertEqual(len(tp["orders"]), 2)
        self.clock.t += 5_000
        lim_buy = self.acc.place("u1", {"client_order_id": "buy-limit-0001", "symbol": "BTC", "side": "buy", "type": "limit", "qty": 0.01, "limit_price": 60_000})
        self.assertEqual(self.pos()["available"], 0.0)  # all of it is reserved by TP/SL
        r = self.acc.close_position("u1", "BTC", "close-cid-oco0")
        self.assertEqual(r["order"]["status"], "filled")
        snap = self.acc.snapshot("u1")
        st = {o["id"]: (o["status"], o["reason"]) for o in snap["orders"]}
        for leg in tp["orders"]:
            self.assertEqual(st[leg["id"]][0], "cancelled")
        self.assertEqual(st[lim_buy["id"]][0], "open")
        self.assertEqual(len(snap["openOrders"]), 1)

    def test_no_position_and_unknown_coin(self):
        with self.assertRaises(OrderError) as c:
            self.acc.close_position("u1", "BTC", "close-cid-none")
        self.assertEqual((c.exception.code, c.exception.status), ("NO_POSITION", 409))
        with self.assertRaises(OrderError) as c:
            self.acc.close_all("u1", "close-all-none")
        self.assertEqual(c.exception.code, "NO_POSITION")
        with self.assertRaises(OrderError) as c:
            self.acc.close_position("u1", "BTC", "bad id!")
        self.assertEqual(c.exception.code, "BAD_CLIENT_ID")

    def test_stale_price_refuses_and_changes_nothing(self):
        self.buy("BTC", 1000)
        self.mkt.ts["BTC"] = self.clock.t - 10 * 60_000
        before = self.acc.snapshot("u1")
        with self.assertRaises(OrderError) as c:
            self.acc.close_position("u1", "BTC", "close-cid-stale")
        self.assertEqual((c.exception.code, c.exception.status), ("STALE_PRICE", 503))
        after = self.acc.snapshot("u1")
        self.assertEqual((before["cash"], before["positions"][0]["qty"]), (after["cash"], after["positions"][0]["qty"]))
        self.mkt.ts.pop("BTC")  # price is fresh again: the same id can be used again (nothing was recorded)
        self.assertEqual(self.acc.close_position("u1", "BTC", "close-cid-stale")["order"]["status"], "filled")

    def test_close_all_closes_every_position_partial_failure_reported(self):
        self.buy("BTC", 1000)
        self.buy("ETH", 500)
        self.mkt.ts["ETH"] = self.clock.t - 10 * 60_000  # ETH price stale
        r = self.acc.close_all("u1", "close-all-0001")
        self.assertEqual([c["symbol"] for c in r["closed"]], ["BTC"])
        self.assertEqual([f["symbol"] for f in r["failed"]], ["ETH"])
        self.assertIsNone(self.pos("BTC"))
        self.assertIsNotNone(self.pos("ETH"))
        self.mkt.ts.pop("ETH")
        r2 = self.acc.close_all("u1", "close-all-0001")  # same id again: BTC replayed, ETH now closed
        by = {c["symbol"]: c["duplicate"] for c in r2["closed"]}
        self.assertEqual(by, {"BTC": True, "ETH": False})
        self.assertEqual(r2["failed"], [])

    def test_close_all_equity_identity(self):
        self.buy("BTC", 1500)
        self.buy("ETH", 700)
        self.mkt.px["BTC"] = 95_000.0
        r = self.acc.close_all("u1", "close-all-0002")
        self.assertEqual(len(r["closed"]), 2)
        s = self.acc.snapshot("u1")
        self.assertEqual([p for p in s["positions"] if p["qty"] > 1e-12], [])
        self.assertAlmostEqual(s["equity"], s["cash"])
        view = PnlView(self.acc, history_fn=lambda *a: [], now_ms=self.clock)
        self.assertAlmostEqual(view.summary("u1")["equity"] - 10_000, view.summary("u1")["realized"], places=6)


class CloseIdempotencyTests(_Base):
    def test_same_client_id_never_sells_twice(self):
        self.buy("BTC", 1000)
        r1 = self.acc.close_position("u1", "BTC", "close-cid-idem")
        cash = self.acc.snapshot("u1")["cash"]
        self.buy("BTC", 500)  # the user buys again afterwards...
        qty = self.pos()["qty"]
        r2 = self.acc.close_position("u1", "BTC", "close-cid-idem")  # ...and a late duplicate request arrives
        self.assertTrue(r2["order"]["duplicate"])
        self.assertEqual(r2["order"]["id"], r1["order"]["id"])
        self.assertAlmostEqual(self.pos()["qty"], qty)  # the new position was not touched
        self.assertEqual(self.acc.snapshot("u1")["cash"], cash - self.acc.snapshot("u1")["orders"][0]["notional"] - self.acc.snapshot("u1")["orders"][0]["fee"])
        sells = [o for o in self.acc.snapshot("u1")["orders"] if o["reason"] == "CLOSE"]
        self.assertEqual(len(sells), 1)

    def test_new_id_on_flat_position_is_conflict_not_second_sale(self):
        self.buy("BTC", 1000)
        self.acc.close_position("u1", "BTC", "close-cid-aaaa")
        with self.assertRaises(OrderError) as c:
            self.acc.close_position("u1", "BTC", "close-cid-bbbb")
        self.assertEqual(c.exception.status, 409)
        self.assertEqual(len([o for o in self.acc.snapshot("u1")["orders"] if o["side"] == "sell"]), 1)

    def test_close_all_replay_is_duplicate(self):
        self.buy("BTC", 1000)
        self.buy("ETH", 500)
        a = self.acc.close_all("u1", "close-all-idem")
        b = self.acc.close_all("u1", "close-all-idem")
        self.assertFalse(a["duplicate"])
        self.assertTrue(b["duplicate"])
        self.assertEqual(sorted(c["id"] for c in a["closed"]), sorted(c["id"] for c in b["closed"]))

    def test_close_id_does_not_collide_with_ordinary_order_ids(self):
        self.buy("BTC", 1000)
        self.n += 1
        self.acc.place("u1", {"client_order_id": "shared-id-0001", "symbol": "ETH", "side": "buy", "type": "market", "notional": 100})
        r = self.acc.close_position("u1", "BTC", "shared-id-0001")
        self.assertFalse(r["order"]["duplicate"])


class CloseOwnershipTests(_Base):
    def test_only_own_positions(self):
        self.buy("BTC", 1000, "alice")
        with self.assertRaises(OrderError) as c:
            self.acc.close_position("bob", "BTC", "close-cid-bob0")
        self.assertEqual(c.exception.code, "NO_POSITION")
        with self.assertRaises(OrderError):
            self.acc.close_all("bob", "close-all-bob0")
        self.assertIsNotNone(self.pos("BTC", "alice"))
        # same id used by two users is two independent requests
        self.buy("BTC", 1000, "bob")
        self.acc.close_position("alice", "BTC", "close-cid-same")
        r = self.acc.close_position("bob", "BTC", "close-cid-same")
        self.assertFalse(r["order"]["duplicate"])
        self.assertIsNone(self.pos("BTC", "bob"))


class CloseConcurrencyTests(_Base):
    def _race(self, calls):
        out, errs, gate = [], [], threading.Barrier(len(calls))

        def run(fn):
            gate.wait()
            try:
                out.append(fn())
            except OrderError as e:
                errs.append(e.code)

        ts = [threading.Thread(target=run, args=(f,)) for f in calls]
        [t.start() for t in ts]
        [t.join() for t in ts]
        return out, errs

    def test_threads_same_id_one_fill(self):
        self.buy("BTC", 1000)
        out, errs = self._race([lambda: self.acc.close_position("u1", "BTC", "close-cid-race") for _ in range(12)])
        self.assertEqual(errs, [])
        self.assertEqual(sum(1 for r in out if not r["order"]["duplicate"]), 1)
        self.assertEqual(len({r["order"]["id"] for r in out}), 1)
        self.assertEqual(len([o for o in self.acc.snapshot("u1")["orders"] if o["reason"] == "CLOSE"]), 1)

    def test_threads_different_ids_one_fill_rest_conflict(self):
        self.buy("BTC", 1000)
        out, errs = self._race([(lambda i=i: self.acc.close_position("u1", "BTC", f"close-cid-r{i:03d}")) for i in range(12)])
        self.assertEqual(len(out), 1)
        self.assertEqual(errs, ["NO_POSITION"] * 11)

    def test_two_connections_to_one_file_like_two_workers(self):
        other = PaperAccounts(self.path, price_fn=self.mkt.price, candles_fn=self.mkt.candles,
                              symbols=lambda: ["BTC", "ETH", "SOL"], limits=Limits(), now_ms=self.clock)
        try:
            self.buy("BTC", 1000)
            calls = [(lambda i=i: (self.acc if i % 2 else other).close_position("u1", "BTC", f"close-cid-w{i:03d}")) for i in range(10)]
            out, errs = self._race(calls)
            self.assertEqual(len(out), 1, (len(out), errs))
            self.assertEqual(set(errs), {"NO_POSITION"})
            self.assertAlmostEqual(self.acc.snapshot("u1")["equity"], self.acc.snapshot("u1")["cash"])
        finally:
            other.close()

    def test_close_all_vs_single_close_race(self):
        self.buy("BTC", 1000)
        self.buy("ETH", 500)
        out, errs = self._race([lambda: self.acc.close_all("u1", "close-all-race"), lambda: self.acc.close_position("u1", "BTC", "close-cid-race2")])
        sells = [o for o in self.acc.snapshot("u1")["orders"] if o["reason"] == "CLOSE" and o["symbol"] == "BTC"]
        self.assertEqual(len(sells), 1)  # BTC sold exactly once whichever request won


class CloseIsolationTests(_Base):
    def test_other_ledgers_untouched_and_no_exchange_code(self):
        d = Path(self.tmp.name)
        others = {}
        for name in ("mainstream_strategy.sqlite", "shadow_h2.json", "shadow_s3.json", "exec_shadow.sqlite", "exchange.sqlite"):
            (d / name).write_bytes(b"ledger-" + name.encode())
            others[name] = hashlib.sha256((d / name).read_bytes()).hexdigest()
        self.buy("BTC", 1000)
        self.buy("ETH", 500)
        self.acc.close_position("u1", "BTC", "close-cid-iso0")
        self.acc.close_all("u1", "close-all-iso0")
        for name, h in others.items():
            self.assertEqual(hashlib.sha256((d / name).read_bytes()).hexdigest(), h, name)
        src = (Path(__file__).resolve().parents[1] / "app" / "paper" / "mainstream_account.py").read_text(encoding="utf-8")
        for bad in ("import ccxt", "app.exchange", "app.live", "strategy_runner", "shadow_h2", "shadow_s3", "exec_shadow"):
            self.assertNotIn(bad, src)


_ENV = ("AUU_AUTH", "AUU_ALLOW_SIGNUP", "AUU_INVITE_CODE", "AUU_ADMIN_USER", "AUU_USER_STORE", "AUU_USER_JOURNAL_DIR", "AUU_AUTH_RATE_MAX", "AUU_LEGACY_PUMP", "AUU_ROLE")
PW = "paperPass123"
P = "/api/v1/mainstream/paper"


class CloseApiTests(_Base):
    def setUp(self):
        super().setUp()
        from app.auth.accounts import reset_accounts as reset_users

        self._prev = {k: os.environ.get(k) for k in _ENV}
        root = Path(self.tmp.name)
        os.environ.update({"AUU_AUTH": "on", "AUU_ALLOW_SIGNUP": "on", "AUU_USER_STORE": str(root / "users.json"),
                           "AUU_USER_JOURNAL_DIR": str(root / "journals"), "AUU_AUTH_RATE_MAX": "1000", "AUU_LEGACY_PUMP": "off"})
        for k in ("AUU_INVITE_CODE", "AUU_ADMIN_USER", "AUU_ROLE"):
            os.environ.pop(k, None)
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

    def test_login_required(self):
        anon = self.TestClient(self.app)
        for path, body in ((f"{P}/positions/BTC/close", {"client_order_id": "api-close-0001"}),
                           (f"{P}/positions/close-all", {"client_order_id": "api-close-0001", "confirm": True})):
            r = anon.post(path, json=body)
            self.assertEqual((r.status_code, r.json()["error"]["code"]), (401, "AUTH_REQUIRED"))
        bad = self.TestClient(self.app, cookies={"auu_session": "forged.9999999999.sig"})
        self.assertEqual(bad.post(f"{P}/positions/BTC/close", json={"client_order_id": "api-close-0001"}).status_code, 401)

    def test_close_flow_idempotent_and_own_only(self):
        alice, bob = self.user("alice"), self.user("bob")
        buy = {"client_order_id": "api-order-0001", "symbol": "BTC", "side": "buy", "type": "market", "notional": 500}
        self.assertEqual(alice.post(f"{P}/orders", json=buy).status_code, 200)
        self.assertEqual(bob.post(f"{P}/positions/BTC/close", json={"client_order_id": "api-close-bob1"}).json()["error"]["code"], "NO_POSITION")
        self.assertEqual(len(alice.get(f"{P}/account").json()["data"]["positions"]), 1)
        body = {"client_order_id": "api-close-0001"}
        r1 = alice.post(f"{P}/positions/BTC/close", json=body)
        r2 = alice.post(f"{P}/positions/btc/close", json=body)
        self.assertEqual((r1.status_code, r2.status_code), (200, 200), r1.text + r2.text)
        self.assertFalse(r1.json()["data"]["order"]["duplicate"])
        self.assertTrue(r2.json()["data"]["order"]["duplicate"])
        acct = alice.get(f"{P}/account").json()["data"]
        self.assertEqual([p for p in acct["positions"] if p["qty"] > 1e-12], [])
        self.assertEqual(len([o for o in acct["orders"] if o["reason"] == "CLOSE"]), 1)
        pnl = alice.get("/api/v1/pnl/summary").json()["data"]
        self.assertEqual(pnl["positions"], [])
        self.assertNotEqual(pnl["realized"], 0)
        r3 = alice.post(f"{P}/positions/BTC/close", json={"client_order_id": "api-close-0002"})
        self.assertEqual((r3.status_code, r3.json()["error"]["code"]), (409, "NO_POSITION"))

    def test_close_all_needs_confirm_and_paper_mode(self):
        c = self.user("carol")
        c.post(f"{P}/orders", json={"client_order_id": "api-order-0001", "symbol": "BTC", "side": "buy", "type": "market", "notional": 500})
        r = c.post(f"{P}/positions/close-all", json={"client_order_id": "api-close-all1"})
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (400, "CONFIRM_REQUIRED"))
        r = c.post(f"{P}/positions/close-all", json={"client_order_id": "api-close-all1", "confirm": True, "mode": "live"})
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (403, "LIVE_API_LOCKED"))
        r = c.post(f"{P}/positions/BTC/close", json={"client_order_id": "api-close-all1", "mode": "live"})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(len(c.get(f"{P}/account").json()["data"]["positions"]), 1)  # nothing closed yet
        r = c.post(f"{P}/positions/close-all", json={"client_order_id": "api-close-all1", "confirm": True})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(len(r.json()["data"]["closed"]), 1)
        self.assertTrue(c.post(f"{P}/positions/close-all", json={"client_order_id": "api-close-all1", "confirm": True}).json()["data"]["duplicate"])

    def test_cross_site_and_unknown_fields_refused(self):
        c = self.user("erin")
        r = c.post(f"{P}/positions/BTC/close", json={"client_order_id": "api-close-0001"}, headers={"Origin": "https://evil.example"})
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (403, "CSRF_ORIGIN"))
        r = c.post(f"{P}/positions/BTC/close", json={"client_order_id": "api-close-0001", "user": "someone-else"})
        self.assertEqual(r.status_code, 422)

    def test_standby_node_refuses_close(self):
        c = self.user("frank")
        os.environ["AUU_ROLE"] = "standby"
        r = c.post(f"{P}/positions/BTC/close", json={"client_order_id": "api-close-0001"})
        self.assertEqual(r.status_code, 503)


if __name__ == "__main__":
    unittest.main()
