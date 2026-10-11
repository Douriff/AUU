"""USDT-TRC20 top-up: unique-tail orders, confirmed-only matching on the official contract,
tx-hash idempotency, review list (mismatch / late / fake contract / already paid), admin 2FA
attribution, reconciliation, closed when no address, primary-only polling."""
from __future__ import annotations

import os
import time
import unittest
from unittest.mock import patch

from app.points import trc20
from app.points.ledger import PointsError, get_points_ledger
from tests.test_points import PW, _Api, key

ADDR = "TEB618Hz3BT4YKsJYLMqbhiZvpBLsrZGtL"  # any valid T-address (test only)
FAKE = "TXLAQ63Xg1NAzckPwKHvzw7CSEmLMEqcdj"   # not the USDT contract
NOW = 1_800_000_000_000


def tx_hash(n: int) -> str:
    return f"{n:064x}"


class FakeChain(trc20.ChainClient):
    """TronGrid stand-in: canned transfers; verify_solid reports per tx."""

    def __init__(self, items=None, solid=None, fail_primary=False, backup_items=None):
        super().__init__(http=self._no_http)
        self.items, self.solid, self.fail_primary, self.backup_items = list(items or []), dict(solid or {}), fail_primary, backup_items
        self.calls = []

    @staticmethod
    def _no_http(*a, **k):
        raise AssertionError("no network in unit tests")

    def incoming_trongrid(self, address, min_ts, pages=5):
        self.calls.append(("trongrid", address, min_ts))
        if self.fail_primary:
            raise RuntimeError("503")
        return [dict(i) for i in self.items]

    def incoming_tronscan(self, address, min_ts):
        self.calls.append(("tronscan", address, min_ts))
        return [dict(i) for i in (self.backup_items or [])]

    def verify_solid(self, tx, address, value):
        return self.solid.get(tx, True)


def transfer(n, micro, ts, contract=trc20.USDT_CONTRACT, symbol="USDT", to=ADDR, source="trongrid", confirmed=True):
    return {"tx": tx_hash(n), "ts": ts, "from": FAKE, "to": to, "contract": contract, "symbol": symbol, "decimals": 6,
            "value": micro, "source": source, "confirmed": confirmed}


class Trc20Base(_Api):
    def setUp(self):
        super().setUp()
        self._env = patch.dict(os.environ, {"AUU_TRC20_RECEIVE_ADDRESS": ADDR})
        self._env.start()
        self.mails = []

    def tearDown(self):
        self._env.stop()
        super().tearDown()

    def notify(self, subject, body):
        self.mails.append((subject, body))

    def order(self, uid=None, usdt=50, now=NOW, idem=None, rand=None):
        kw = {"now": now}
        if rand:
            kw["rand"] = rand
        row, _ = trc20.create_order(uid or self.uid, usdt, idem or key(time.time_ns()), **kw)
        return row

    def poll(self, chain, now):
        return trc20.poll_once(chain, now=now, notify=self.notify)


class AddressTests(unittest.TestCase):
    def test_base58check(self):
        self.assertTrue(trc20.valid_address(trc20.USDT_CONTRACT))
        self.assertTrue(trc20.valid_address(ADDR))
        for bad in ("", "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6x", "0x" + "a" * 40, "1BoatSLRHtKNngkdXEeobR76b53LETtpyT", "T" * 34):
            self.assertFalse(trc20.valid_address(bad), bad)
        self.assertEqual(trc20.from_hex20(trc20.hex20(ADDR)), ADDR)

    def test_amount_format(self):
        self.assertEqual(trc20.fmt_order_amount(50_013_700), "50.0137")
        self.assertEqual(trc20.fmt_order_amount(10_000_100), "10.0001")


class ClosedTests(_Api):
    def test_closed_without_address(self):
        for v in ("", "not-an-address", "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6x"):
            with patch.dict(os.environ, {"AUU_TRC20_RECEIVE_ADDRESS": v}):
                d = self.user.get("/api/v1/points/trc20").json()["data"]
                self.assertFalse(d["config"]["enabled"])
                self.assertIsNone(d["config"]["address"])
                me = self.user.get("/api/v1/points/me").json()["data"]
                self.assertFalse(me["topup"]["open"])
                r = self.user.post("/api/v1/points/trc20/orders", json={"usdt": 50, "idempotency_key": key(1)})
                self.assertEqual(r.json()["error"]["code"], "POINTS_TOPUP_CLOSED")
                self.assertFalse(trc20.poll_enabled())

    def test_no_address_write_endpoint(self):
        paths = list(self.app.openapi()["paths"])
        self.assertFalse([p for p in paths if "trc20" in p and ("address" in p or "config" in p)])


class OrderTests(Trc20Base):
    def test_order_api_shape_limits_and_idempotency(self):
        r = self.user.post("/api/v1/points/trc20/orders", json={"usdt": 50, "idempotency_key": key("o1")})
        self.assertEqual(r.status_code, 200, r.text)
        o = r.json()["data"]["order"]
        self.assertEqual((o["usdt"], o["points"], o["address"], o["status"], o["network"]), (50, 5000, ADDR, "pending", "TRC20"))
        self.assertRegex(o["amount"], r"^50\.\d{4}$")
        self.assertNotEqual(o["amount"], "50.0000")
        self.assertTrue(o["qr"].startswith("data:image/png;base64,"))
        self.assertTrue(1790 <= o["secondsLeft"] <= 1800)
        again = self.user.post("/api/v1/points/trc20/orders", json={"usdt": 50, "idempotency_key": key("o1")}).json()["data"]
        self.assertTrue(again["replayed"])
        self.assertEqual(again["order"]["amount"], o["amount"])
        for bad in (9, 1001, "50.5", 50.5, True, "abc", None):
            r = self.user.post("/api/v1/points/trc20/orders", json={"usdt": bad, "idempotency_key": key(f"b{bad}")})
            self.assertEqual(r.json()["error"]["code"], "TRC20_BAD_AMOUNT", bad)
        # pending cap (3)
        for i in range(2):
            self.assertEqual(self.user.post("/api/v1/points/trc20/orders", json={"usdt": 10, "idempotency_key": key(f"p{i}")}).status_code, 200)
        r = self.user.post("/api/v1/points/trc20/orders", json={"usdt": 10, "idempotency_key": key("p9")})
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (429, "TRC20_TOO_MANY_PENDING"))
        # other user's order is not visible
        other = self.admin.get(f"/api/v1/points/trc20/orders/{o['orderId']}")
        self.assertEqual(other.status_code, 200)  # admin may look
        hist = self.user.get("/api/v1/points/trc20").json()["data"]
        self.assertEqual(len(hist["orders"]), 3)
        self.assertEqual(hist["config"]["rate"], 100)

    def test_other_user_cannot_read_order(self):
        o = self.order(uid="someone-else", now=int(time.time() * 1000))
        r = self.user.get(f"/api/v1/points/trc20/orders/{o['order_id']}")
        self.assertEqual(r.json()["error"]["code"], "POINTS_UNKNOWN_ORDER")

    def test_daily_limit(self):
        with patch.dict(os.environ, {"AUU_TRC20_DAILY_MAX": "100", "AUU_TRC20_MAX_PENDING": "10"}):
            self.order(usdt=60)
            with self.assertRaises(PointsError) as cm:
                self.order(usdt=50)
            self.assertEqual(str(cm.exception), "TRC20_DAILY_LIMIT")
            self.order(usdt=40)

    def test_tail_collision_avoided_and_reuse_blocked_24h(self):
        seq = iter([136, 136, 136, 5])  # rand(9999) -> tails 137,137,137,6
        rnd = lambda n: next(seq)  # noqa: E731
        a = self.order(uid="u1", usdt=50, rand=rnd)
        b = self.order(uid="u2", usdt=50, now=NOW + 1000, rand=rnd)
        self.assertEqual(a["amount_micro"], 50_013_700)
        self.assertNotEqual(a["amount_micro"], b["amount_micro"])
        self.assertEqual(b["amount_micro"], 50_000_600)
        # even after a's 30 min expiry the same tail is not handed out within 24 h
        c = self.order(uid="u3", usdt=50, now=NOW + 3600_000, rand=lambda n: 136 if n == 9999 else 0)
        self.assertNotEqual(c["amount_micro"], 50_013_700)
        d = self.order(uid="u4", usdt=50, now=NOW + 25 * 3600_000, rand=lambda n: 136)
        self.assertEqual(d["amount_micro"], 50_013_700)

    def test_standby_refuses_orders(self):
        with patch.dict(os.environ, {"AUU_ROLE": "standby"}):
            r = self.user.post("/api/v1/points/trc20/orders", json={"usdt": 50, "idempotency_key": key("s1")})
            self.assertEqual(r.status_code, 503)
            self.assertFalse(self.user.get("/api/v1/points/trc20").json()["data"]["config"]["enabled"])
            self.assertFalse(trc20.poll_enabled())


class MatchTests(Trc20Base):
    def bal(self, uid=None):
        return get_points_ledger().balance(uid or self.uid)

    def test_match_credits_once_and_mails(self):
        o = self.order()
        t = transfer(1, o["amount_micro"], NOW + 600_000)
        chain = FakeChain([t])
        r = self.poll(chain, NOW + 700_000)
        self.assertEqual(r["results"], {"matched": 1})
        self.assertEqual(self.bal(), 5000)
        self.assertEqual(trc20.get_order(o["order_id"])["status"], "paid")
        self.assertEqual(trc20.get_order(o["order_id"])["tx_hash"], tx_hash(1))
        # same tx again (next poll, and a duplicate row in one response): never twice
        chain.items = [t, t]
        self.poll(chain, NOW + 760_000)
        self.assertEqual(self.bal(), 5000)
        self.assertEqual(len([m for m in self.mails if "到账" in m[0]]), 1)
        self.assertIn(ADDR, self.mails[0][1])
        self.assertIn(tx_hash(1), self.mails[0][1])
        rec = trc20.reconcile()
        self.assertTrue(rec["balanced"])
        self.assertEqual((rec["chainTotal"], rec["creditedTotal"], rec["pendingTotal"]), (trc20.fmt_usdt(o["amount_micro"]),) * 2 + ("0",))
        self.assertTrue(rec["pointsMatchLedger"])
        self.assertTrue(get_points_ledger().verify()["ok"])

    def test_crash_between_credit_and_mark_is_safe(self):
        o = self.order()
        t = transfer(2, o["amount_micro"], NOW + 60_000)
        trc20._credit(t["tx"], self.uid, o["points"], operator="provider:usdt_trc20", reason=f"USDT-TRC20 {trc20.fmt_order_amount(o['amount_micro'])}",
                      ref=f"tx:{t['tx']}")  # credited, then "crash" before the tx/order rows were written
        self.poll(FakeChain([t]), NOW + 200_000)
        self.assertEqual(self.bal(), 5000)
        self.assertEqual(trc20.get_order(o["order_id"])["status"], "paid")

    def test_unconfirmed_young_and_unsolidified_wait(self):
        o = self.order()
        t = transfer(3, o["amount_micro"], NOW + 100_000)
        self.assertEqual(self.poll(FakeChain([dict(t, confirmed=False)]), NOW + 300_000)["results"], {"unconfirmed": 1})
        self.assertEqual(self.poll(FakeChain([t]), NOW + 130_000)["results"], {"young": 1})
        self.assertEqual(self.poll(FakeChain([t], solid={t["tx"]: None}), NOW + 300_000)["results"], {"unverified": 1})
        self.assertEqual(self.bal(), 0)
        self.assertEqual(self.poll(FakeChain([t]), NOW + 400_000)["results"], {"matched": 1})
        self.assertEqual(self.bal(), 5000)

    def test_failed_solidity_check_goes_to_review(self):
        o = self.order()
        t = transfer(4, o["amount_micro"], NOW + 100_000)
        self.assertEqual(self.poll(FakeChain([t], solid={t["tx"]: False}), NOW + 300_000)["results"], {"verify_failed": 1})
        self.assertEqual(self.bal(), 0)

    def test_amount_mismatch(self):
        o = self.order()
        t = transfer(5, o["amount_micro"] + 100, NOW + 100_000)  # one tail step off
        self.assertEqual(self.poll(FakeChain([t]), NOW + 300_000)["results"], {"amount_mismatch": 1})
        self.assertEqual(self.bal(), 0)
        self.assertEqual(trc20.txs("exception")[0]["reason"], "amount_mismatch")
        self.assertIn("待人工处理", self.mails[0][0])
        rec = trc20.reconcile()
        self.assertTrue(rec["balanced"])
        self.assertEqual(rec["pendingCount"], 1)

    def test_late_payment(self):
        o = self.order()
        t = transfer(6, o["amount_micro"], o["expires_at"] + 1000)
        self.assertEqual(self.poll(FakeChain([t]), o["expires_at"] + 300_000)["results"], {"late": 1})
        self.assertEqual(self.bal(), 0)
        row = trc20.txs("exception")[0]
        self.assertEqual((row["reason"], row["order_id"], row["user_id"]), ("late", o["order_id"], self.uid))
        self.assertEqual(trc20.order_view(trc20.get_order(o["order_id"]), now=o["expires_at"] + 400_000)["status"], "expired")

    def test_paid_inside_window_detected_after_expiry_still_credits(self):
        o = self.order()
        t = transfer(7, o["amount_micro"], o["expires_at"] - 5_000)
        trc20.expire_due(o["expires_at"] + 3600_000)
        self.assertEqual(trc20.get_order(o["order_id"])["status"], "expired")
        self.assertEqual(self.poll(FakeChain([t]), o["expires_at"] + 3600_000)["results"], {"matched": 1})
        self.assertEqual(self.bal(), 5000)

    def test_second_payment_of_paid_order(self):
        o = self.order()
        a = transfer(8, o["amount_micro"], NOW + 100_000)
        b = transfer(9, o["amount_micro"], NOW + 200_000)
        self.assertEqual(self.poll(FakeChain([a, b]), NOW + 400_000)["results"], {"matched": 1, "already_paid": 1})
        self.assertEqual(self.bal(), 5000)

    def test_fake_usdt_contract(self):
        o = self.order()
        fake = transfer(10, o["amount_micro"], NOW + 100_000, contract=FAKE, symbol="USDT")
        spam = transfer(11, 123, NOW + 100_000, contract=FAKE, symbol="AIRDROP")
        self.assertEqual(self.poll(FakeChain([fake, spam]), NOW + 300_000)["results"], {"fake_contract": 1, "ignored_token": 1})
        self.assertEqual(self.bal(), 0)
        self.assertEqual(trc20.get_order(o["order_id"])["status"], "pending")
        rec = trc20.reconcile()
        self.assertEqual((rec["chainTotal"], rec["fakeContractCount"]), ("0", 1))

    def test_wrong_recipient_ignored(self):
        o = self.order()
        self.assertEqual(self.poll(FakeChain([transfer(12, o["amount_micro"], NOW + 1, to=FAKE)]), NOW + 300_000)["results"], {"skip": 1})

    def test_backup_source_when_trongrid_down(self):
        o = self.order()
        t = transfer(13, o["amount_micro"], NOW + 100_000, source="tronscan")
        chain = FakeChain(fail_primary=True, backup_items=[t], solid={t["tx"]: None})
        r = self.poll(chain, NOW + 300_000)
        self.assertEqual((r["source"], r["results"]), ("tronscan", {"matched": 1}))
        self.assertEqual(self.bal(), 5000)
        # tronscan says not confirmed -> wait
        o2 = self.order(usdt=20)
        t2 = transfer(14, o2["amount_micro"], NOW + 100_000, source="tronscan", confirmed=False)
        self.poll(FakeChain(fail_primary=True, backup_items=[t2], solid={t2["tx"]: None}), NOW + 300_000)
        self.assertEqual(self.bal(), 5000)

    def test_poll_failure_records_error(self):
        class Down(FakeChain):
            def incoming_tronscan(self, address, min_ts):
                raise RuntimeError("401")
        r = self.poll(Down(fail_primary=True), NOW)
        self.assertFalse(r["ok"])
        self.assertIn("401", trc20.reconcile()["lastError"]["error"])


class AdminTests(Trc20Base):
    def test_resolve_needs_2fa_reason_and_credits_once(self):
        o = self.order()
        t = transfer(20, o["amount_micro"] + 500, NOW + 100_000)
        self.poll(FakeChain([t]), NOW + 300_000)
        d = self.admin.get("/api/v1/admin/points/trc20").json()["data"]
        self.assertEqual(len(d["pending"]), 1)
        self.assertEqual(d["pending"][0]["url"], f"https://tronscan.org/#/transaction/{t['tx']}")
        self.assertEqual(d["orders"][0]["userName"], "alice")
        self.assertTrue(d["reconcile"]["balanced"])
        self.assertEqual(self.user.get("/api/v1/admin/points/trc20").status_code, 403)
        body = {"tx_hash": t["tx"], "user_id": self.uid, "points": 5000, "reason": "", "password": PW, "totp_code": "000000"}
        r = self.admin.post("/api/v1/admin/points/trc20/resolve", json=body, headers={"Idempotency-Key": key("r1")})
        self.assertEqual(r.json()["error"]["code"], "POINTS_REASON_REQUIRED")
        body["reason"] = "用户少转了尾数，核对付款截图后归属"
        r = self.admin.post("/api/v1/admin/points/trc20/resolve", json=body, headers={"Idempotency-Key": key("r1")})
        self.assertEqual(r.json()["error"]["code"], "POINTS_2FA_REQUIRED")
        self.enable_2fa()
        body["totp_code"] = "000000"
        r = self.admin.post("/api/v1/admin/points/trc20/resolve", json=body, headers={"Idempotency-Key": key("r1")})
        self.assertEqual(r.status_code, 401)
        body["totp_code"] = self.code()
        r = self.admin.post("/api/v1/admin/points/trc20/resolve", json=body, headers={"Idempotency-Key": key("r1")})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(get_points_ledger().balance(self.uid), 5000)
        body["totp_code"] = self.code()
        r = self.admin.post("/api/v1/admin/points/trc20/resolve", json=body, headers={"Idempotency-Key": key("r2")})
        self.assertEqual(r.json()["error"]["code"], "TRC20_NOT_PENDING")
        self.assertEqual(get_points_ledger().balance(self.uid), 5000)
        rec = trc20.reconcile()
        self.assertTrue(rec["balanced"])
        self.assertEqual((rec["creditedManual"], rec["pendingCount"]), (trc20.fmt_usdt(o["amount_micro"] + 500), 0))
        audit = get_points_ledger().audit_rows(20)
        self.assertTrue(any(a["action"] == "trc20_resolve" and a["result"] == "ok" for a in audit))

    def test_dismiss(self):
        self.order()
        t = transfer(21, 1_234_500, NOW + 100_000)
        self.poll(FakeChain([t]), NOW + 300_000)
        self.enable_2fa()
        r = self.admin.post("/api/v1/admin/points/trc20/dismiss",
                            json={"tx_hash": t["tx"], "reason": "测试转账，已线下退回", "password": PW, "totp_code": self.code()})
        self.assertEqual(r.status_code, 200, r.text)
        rec = trc20.reconcile()
        self.assertEqual((rec["ignoredTotal"], rec["pendingCount"], rec["balanced"]), ("1.2345", 0, True))


@unittest.skipUnless(os.getenv("AUU_TRC20_LIVE") == "1", "set AUU_TRC20_LIVE=1 for the read-only TronGrid check")
class LiveTronGridTests(unittest.TestCase):
    """Read-only: real TronGrid against a public, busy address (no keys, no writes)."""

    def test_live_incoming_and_solidity(self):
        addr = os.getenv("AUU_TRC20_LIVE_ADDRESS", "TEB618Hz3BT4YKsJYLMqbhiZvpBLsrZGtL")
        c = trc20.ChainClient()
        items = c.incoming_trongrid(addr, int(time.time() * 1000) - 7 * 86400_000, pages=1)
        usdt = [i for i in items if i["contract"] == trc20.USDT_CONTRACT]
        self.assertTrue(usdt, "no USDT transfers in the last 7 days for the probe address")
        t = usdt[0]
        self.assertEqual(t["to"], addr)
        self.assertEqual(c.verify_solid(t["tx"], addr, t["value"]), True)
        self.assertEqual(c.verify_solid(t["tx"], addr, t["value"] + 1), False)
        print(f"\nlive: {len(items)} transfers, first USDT {t['tx']} {trc20.fmt_usdt(t['value'])} USDT")


if __name__ == "__main__":
    unittest.main()
