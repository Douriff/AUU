"""Points (积分): append-only ledger, idempotency, server-side amounts, admin 2FA step-up, channels,
CSV export, audit log, and full separation from the paper ledgers."""
from __future__ import annotations

import csv
import io
import os
import re
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import pyotp

from app.auth.accounts import reset_accounts
from app.points import catalog, providers, service
from app.points.ledger import PointsError, PointsLedger, entry_hash, reset_points_ledger

_KEYS = ("AUU_AUTH", "AUU_ALLOW_SIGNUP", "AUU_INVITE_CODE", "AUU_ADMIN_USER", "AUU_USER_STORE", "AUU_USER_JOURNAL_DIR",
         "AUU_AUTH_RATE_MAX", "DATA_PROVIDER", "PUMP_PAPER_LOOP", "PUMPFUN_DISCOVERY", "AUU_POINTS_STORE", "AUU_POINTS_MOCK",
         "AUU_POINTS_MANUAL", "AUU_POINTS_SHOP", "AUU_POINTS_PROVIDERS", "AUU_ROLE")
PW = "password123"
UA = {"user-agent": "UnitTest/1.0"}
K = "k" * 20


def key(n: int | str) -> str:
    return f"idem-{n}".ljust(20, "x")


class LedgerUnitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.led = PointsLedger(Path(self.tmp.name) / "points.sqlite")

    def tearDown(self):
        self.led.close()
        self.tmp.cleanup()

    def add(self, kind="adjust", amount=10, idem="a", fp="f", user="u1", **kw):
        return self.led.append(user_id=user, kind=kind, amount=amount, reason="test reason", operator="admin:x", idem_key=idem,
                               fingerprint=fp, **kw)

    def test_append_only_balance_and_chain(self):
        r1, rep = self.add(amount=50, idem="1")
        self.assertFalse(rep)
        r2, _ = self.add(kind="spend", amount=-20, idem="2")
        self.add(user="u2", kind="topup", amount=7, idem="3")
        self.assertEqual(self.led.balance("u1"), 30)
        self.assertEqual((r1["balance_after"], r2["balance_after"]), (50, 30))
        self.assertEqual(r2["prev_hash"], r1["hash"])
        v = self.led.verify()
        self.assertTrue(v["ok"], v)
        self.assertEqual((v["entries"], v["users"], v["total"]), (3, 2, 37))
        db = sqlite3.connect(self.led.path, isolation_level=None)
        for sql in ("UPDATE points_ledger SET amount=999 WHERE id=1", "DELETE FROM points_ledger WHERE id=1",
                    "UPDATE points_audit SET result='x'", "DELETE FROM points_audit"):
            self.led.audit(actor="a", action="b", result="c")
            with self.assertRaises(sqlite3.DatabaseError):
                db.execute(sql)
        db.close()
        self.assertEqual(self.led.balance("u1"), 30)

    def test_tamper_is_detected(self):
        self.add(amount=50, idem="1")
        self.add(amount=5, idem="2")
        db = sqlite3.connect(self.led.path)
        db.execute("DROP TRIGGER points_ledger_no_update")  # someone with file access bypasses the trigger
        db.execute("UPDATE points_ledger SET amount=500, balance_after=500 WHERE id=1")
        db.commit()
        db.close()
        v = self.led.verify()
        self.assertFalse(v["ok"])
        self.assertTrue(any("hash mismatch" in p for p in v["problems"]), v)

    def test_idempotency_replay_and_conflict(self):
        r1, rep1 = self.add(amount=10, idem="same", fp="A")
        r2, rep2 = self.add(amount=10, idem="same", fp="A")
        self.assertEqual((rep1, rep2, r1["id"]), (False, True, r2["id"]))
        self.assertEqual(self.led.balance("u1"), 10)
        with self.assertRaises(PointsError) as cm:
            self.add(amount=99, idem="same", fp="B")
        self.assertEqual(str(cm.exception), "POINTS_IDEMPOTENCY_CONFLICT")

    def test_signs_bounds_and_no_negative_balance(self):
        for kind, amount in (("topup", -1), ("spend", 5), ("adjust", 0), ("topup", 10_000_001), ("topup", True), ("topup", 1.5), ("bogus", 1)):
            with self.assertRaises(PointsError):
                self.add(kind=kind, amount=amount, idem=f"{kind}{amount}")
        with self.assertRaises(PointsError) as cm:
            self.add(kind="spend", amount=-1, idem="neg")
        self.assertEqual(str(cm.exception), "POINTS_INSUFFICIENT")
        with self.assertRaises(PointsError):
            self.led.append(user_id="u1", kind="adjust", amount=1, reason="  ", operator="x", idem_key="r", fingerprint="f")
        self.assertEqual(self.led.entries("u1"), [])

    def test_entry_hash_covers_every_field(self):
        r, _ = self.add(amount=3, idem="h")
        for k in ("user_id", "amount", "reason", "operator", "ts", "kind"):
            changed = dict(r, **{k: (r[k] + 1) if isinstance(r[k], int) else str(r[k]) + "x"})
            self.assertNotEqual(entry_hash(changed), r["hash"], k)


class ParseTests(unittest.TestCase):
    def test_amounts(self):
        self.assertEqual(service.parse_amount(5), 5)
        self.assertEqual(service.parse_amount(" 12 "), 12)
        for bad in (0, -1, 1.5, "1e3", "1.0", "", None, True, "abc", [], {}, 10**12, "-5"):
            with self.assertRaises(PointsError, msg=repr(bad)):
                service.parse_amount(bad)
        with self.assertRaises(PointsError):
            service.parse_amount(100_001, hi=service.TOPUP_MAX)
        self.assertEqual(service.parse_signed("-30"), -30)
        for bad in (0, "0", 1_000_001, -1_000_001, 2.0, False):
            with self.assertRaises(PointsError, msg=repr(bad)):
                service.parse_signed(bad)

    def test_reason_and_idem(self):
        self.assertEqual(service.parse_reason("  补偿\n活动  奖励 \x00"), "补偿 活动 奖励")
        for bad in ("", " ", "x", None, 5):
            with self.assertRaises(PointsError):
                service.parse_reason(bad)
        with self.assertRaises(PointsError):
            service.parse_reason("y" * 201)
        for bad in ("short", "has space in it here", None, "x" * 81, "ünïcode-key-1234567"):
            with self.assertRaises(PointsError):
                service.parse_idem(bad)
        self.assertEqual(service.parse_idem(K), K)
        self.assertRegex(service.new_idempotency_key(), r"^[A-Za-z0-9_-]{16,80}$")


class ProviderTests(unittest.TestCase):
    def test_defaults_closed_and_reserved_slots_unimplemented(self):
        with patch.dict(os.environ, {"AUU_POINTS_PROVIDERS": "alipay,wechat_pay,stripe,mock,manual_admin"}):
            os.environ.pop("AUU_POINTS_MOCK", None)
            os.environ.pop("AUU_POINTS_MANUAL", None)
            st = providers.topup_status()
            self.assertFalse(st["open"])
            self.assertEqual(st["channels"], [])
            self.assertFalse(st["manual"])
            ids = {p["id"]: p for p in st["providers"]}
            self.assertNotIn("mock", ids)  # test double never listed unless enabled under tests
            for pid in ("alipay", "wechat_pay", "stripe"):
                self.assertFalse(ids[pid]["enabled"])
                self.assertFalse(ids[pid]["implemented"])
                with self.assertRaises(NotImplementedError):
                    providers.get_provider(pid).create_order(order_id="o", user_id="u", points=1)
                with self.assertRaises(NotImplementedError):
                    providers.get_provider(pid).parse_webhook({}, b"")

    def test_mock_is_test_only(self):
        with patch.dict(os.environ, {"AUU_POINTS_MOCK": "on"}):
            self.assertTrue(providers.get_provider("mock").enabled())  # AUU_TEST=1 under the test runner
            with patch.object(providers, "tests_active", lambda: False):
                self.assertFalse(providers.get_provider("mock").enabled())
                self.assertEqual(providers.user_channels(), [])

    def test_no_secrets_in_points_code(self):
        root = Path(__file__).resolve().parents[1] / "app"
        text = "".join(p.read_text(encoding="utf-8") for p in list((root / "points").glob("*.py")) + [root / "routes" / "points.py"])
        self.assertNotRegex(text, r"(?i)(sk_live|sk_test|private[_ ]?key\s*=|app_secret\s*=|BEGIN (RSA )?PRIVATE)")


class _Api(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.root = root
        self._prev = {k: os.environ.get(k) for k in _KEYS}
        os.environ.update({"AUU_AUTH": "on", "AUU_ALLOW_SIGNUP": "on", "AUU_USER_STORE": str(root / "users.json"),
                           "AUU_USER_JOURNAL_DIR": str(root / "journals"), "AUU_AUTH_RATE_MAX": "1000",
                           "DATA_PROVIDER": "mock", "PUMP_PAPER_LOOP": "0", "PUMPFUN_DISCOVERY": "off",
                           "AUU_POINTS_STORE": str(root / "points.sqlite")})
        for k in ("AUU_INVITE_CODE", "AUU_ADMIN_USER", "AUU_POINTS_MOCK", "AUU_POINTS_MANUAL", "AUU_POINTS_SHOP", "AUU_POINTS_PROVIDERS", "AUU_ROLE"):
            os.environ.pop(k, None)
        reset_accounts()
        reset_points_ledger()
        from fastapi.testclient import TestClient
        from app.main import app

        self.TC, self.app = TestClient, app
        self.admin = TestClient(app, headers=UA)
        self.assertEqual(self.reg(self.admin, "boss").status_code, 200)  # first account = admin
        self.user = TestClient(app, headers=UA)
        r = self.reg(self.user, "alice")
        self.uid = r.json()["data"]["user"]["id"]
        self.assertFalse(r.json()["data"]["user"]["is_admin"])

    def tearDown(self):
        for k, v in self._prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        reset_accounts()
        reset_points_ledger()
        self.tmp.cleanup()

    @staticmethod
    def reg(client, name):
        return client.post("/api/v1/auth/register", json={"name": name, "password": PW, "password_confirm": PW})

    def enable_2fa(self):
        s = self.admin.post("/api/v1/auth/2fa/setup", json={"password": PW}).json()["data"]["secret"]
        e = self.admin.post("/api/v1/auth/2fa/enable", json={"code": pyotp.TOTP(s).now()})
        self.assertEqual(e.status_code, 200, e.text)
        self.secret, self.recovery = s, list(e.json()["data"]["recovery_codes"])
        self.fresh = [pyotp.TOTP(s).at(time.time() + 30)]

    def code(self):
        return self.fresh.pop() if self.fresh else self.recovery.pop()

    def adjust(self, amount, reason="活动补偿 #1", idem=None, code=None, pw=PW, user_id=None):
        body = {"user_id": user_id or self.uid, "amount": amount, "reason": reason, "password": pw,
                "totp_code": code if code is not None else self.code()}
        return self.admin.post("/api/v1/admin/points/adjust", json=body, headers={"Idempotency-Key": idem or key(time.time_ns())})


class UserApiTests(_Api):
    def test_balance_ledger_and_closed_topup(self):
        me = self.user.get("/api/v1/points/me")
        self.assertEqual(me.status_code, 200, me.text)
        d = me.json()["data"]
        self.assertEqual((d["balance"], d["entries"], d["topup"]["open"]), (0, [], False))
        self.assertEqual(d["rules"], {"withdraw": False, "transfer": False, "separateFromPaper": True})
        self.assertFalse(d["shop"]["enabled"])
        self.assertFalse(d["entitlements"]["example_badge"]["active"])
        r = self.user.post("/api/v1/points/topup", json={"provider": "alipay", "points": 100, "idempotency_key": K})
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (403, "POINTS_TOPUP_CLOSED"))
        r = self.user.post("/api/v1/points/spend", json={"item": "example_badge", "idempotency_key": K})
        self.assertEqual(r.json()["error"]["code"], "POINTS_SHOP_OFF")

    def test_no_withdraw_or_transfer_routes(self):
        paths = [getattr(r, "path", "") for r in self.app.routes]
        self.assertTrue(any(p.startswith("/api/v1/points/") for p in paths))
        self.assertFalse([p for p in paths if re.search(r"(?i)withdraw|transfer|cashout|payout", p)])

    def test_login_and_admin_required(self):
        anon = self.TC(self.app, headers=UA)
        self.assertEqual(anon.get("/api/v1/points/me").status_code, 401)
        for path in ("/api/v1/admin/points/users", f"/api/v1/admin/points/user/{self.uid}", "/api/v1/admin/points/export.csv",
                     "/api/v1/admin/points/verify", "/api/v1/admin/points/audit"):
            self.assertEqual(self.user.get(path).status_code, 403, path)
        r = self.user.post("/api/v1/admin/points/adjust", json={"user_id": self.uid, "amount": 5, "reason": "self service"})
        self.assertEqual(r.status_code, 403)
        with patch.dict(os.environ, {"AUU_AUTH": "off"}):
            self.assertEqual(self.user.get("/api/v1/points/me").json()["error"]["code"], "AUTH_OFF")

    def test_cross_site_write_refused(self):
        r = self.user.post("/api/v1/points/spend", json={"item": "example_badge", "idempotency_key": K},
                           headers={"Origin": "https://evil.example"})
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (403, "CSRF_ORIGIN"))

    def test_standby_refuses_points_writes(self):
        with patch.dict(os.environ, {"AUU_ROLE": "standby"}):
            r = self.user.post("/api/v1/points/spend", json={"item": "example_badge", "idempotency_key": K})
            self.assertEqual(r.status_code, 503)
            self.assertEqual(self.admin.post("/api/v1/admin/points/adjust", json={}).status_code, 503)
            self.assertEqual(self.user.get("/api/v1/points/me").status_code, 200)


class AdminApiTests(_Api):
    def test_adjust_needs_2fa_reason_and_is_idempotent(self):
        r = self.adjust(100, code="123456")
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (403, "POINTS_2FA_REQUIRED"))
        self.enable_2fa()
        c = self.code()
        r = self.adjust(100, reason=" ", code=c)
        self.assertEqual(r.json()["error"]["code"], "POINTS_REASON_REQUIRED")
        r = self.adjust("1.5", code=c)
        self.assertEqual(r.json()["error"]["code"], "POINTS_BAD_AMOUNT")
        r = self.adjust(100, code=c, pw="wrongpass1")
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (401, "BAD_LOGIN"))
        r = self.adjust(100, code="000000")
        self.assertEqual(r.json()["error"]["code"], "TOTP_BAD")
        r = self.adjust(100, code="")
        self.assertEqual(r.json()["error"]["code"], "TOTP_REQUIRED")
        r = self.adjust(100, code=c, idem=key("first"))  # validation failures above did not burn this code
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()["data"]
        self.assertEqual((d["balance"], d["replayed"], d["entry"]["kind"], d["entry"]["operator"].split(":")[0]), (100, False, "adjust", "admin"))
        r = self.adjust(100, idem=key("first"))  # network retry: same key, same request
        self.assertEqual((r.status_code, r.json()["data"]["replayed"], r.json()["data"]["balance"]), (200, True, 100))
        r = self.adjust(999, idem=key("first"))
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "POINTS_IDEMPOTENCY_CONFLICT"))
        r = self.adjust(-50, code=c)  # the TOTP step used above cannot be replayed
        self.assertEqual(r.json()["error"]["code"], "TOTP_BAD")
        self.assertEqual(self.user.get("/api/v1/points/me").json()["data"]["balance"], 100)

    def test_negative_adjust_csv_audit_and_verify(self):
        self.enable_2fa()
        self.assertEqual(self.adjust(30, reason="=HYPERLINK(\"http://x\")").status_code, 200)
        r = self.adjust(-31)
        self.assertEqual(r.json()["error"]["code"], "POINTS_INSUFFICIENT")
        self.assertEqual(self.adjust(-10, reason="扣回误发").json()["data"]["balance"], 20)
        r = self.adjust(5, user_id="nope")
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (404, "UNKNOWN_USER"))
        me = self.user.get("/api/v1/points/me").json()["data"]
        self.assertEqual(me["balance"], 20)
        self.assertEqual([e["amount"] for e in me["entries"]], [-10, 30])
        self.assertNotIn("operator", me["entries"][0])  # users see the kind of operator, not admin ids
        self.assertEqual(me["entries"][0]["operatorKind"], "admin")
        look = self.admin.get(f"/api/v1/admin/points/user/{self.uid}").json()["data"]
        self.assertEqual((look["balance"], look["user"]["name"]), (20, "alice"))
        res = self.admin.get(f"/api/v1/admin/points/export.csv?user_id={self.uid}")
        self.assertEqual(res.status_code, 200)
        self.assertIn("attachment", res.headers["content-disposition"])
        text = res.content.decode("utf-8-sig")
        rows = list(csv.DictReader(io.StringIO(text)))
        self.assertEqual([r["amount"] for r in rows], ["30", "-10"])
        self.assertTrue(rows[0]["reason"].startswith("'="))  # formula neutralised
        self.assertEqual(rows[1]["user_name"], "alice")
        self.assertTrue(rows[0]["time_bj"].endswith("+08:00"))
        allcsv = self.admin.get("/api/v1/admin/points/export.csv").content.decode("utf-8-sig")
        self.assertEqual(len(list(csv.DictReader(io.StringIO(allcsv)))), 2)
        v = self.admin.get("/api/v1/admin/points/verify").json()["data"]
        self.assertTrue(v["ok"], v)
        self.assertEqual(v["total"], 20)
        audit = self.admin.get("/api/v1/admin/points/audit?limit=100").json()["data"]["items"]
        results = [(a["action"], a["result"]) for a in audit]
        self.assertIn(("adjust", "ok"), results)
        self.assertIn(("adjust", "POINTS_INSUFFICIENT"), results)
        self.assertIn(("export_csv", "ok"), results)
        self.assertIn(("lookup", "ok"), results)
        blob = str(audit)
        self.assertNotIn(PW, blob)
        self.assertNotIn("totp_code", blob)
        users = self.admin.get("/api/v1/admin/points/users?q=ali").json()["data"]["items"]
        self.assertEqual([(u["name"], u["balance"]) for u in users], [("alice", 20)])

    def test_manual_channel_off_by_default_then_on(self):
        self.enable_2fa()
        body = {"user_id": self.uid, "points": 500, "reason": "线下转账 10 月", "external_ref": "bank-123", "password": PW,
                "totp_code": "000000"}
        r = self.admin.post("/api/v1/admin/points/manual-topup", json=body, headers={"Idempotency-Key": key("m1")})
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (403, "POINTS_MANUAL_OFF"))
        with patch.dict(os.environ, {"AUU_POINTS_MANUAL": "on"}):
            body["totp_code"] = self.code()
            r = self.admin.post("/api/v1/admin/points/manual-topup", json=body, headers={"Idempotency-Key": key("m1")})
            self.assertEqual(r.status_code, 200, r.text)
            e = r.json()["data"]["entry"]
            self.assertEqual((e["kind"], e["amount"], e["provider"], e["ref"]), ("topup", 500, "manual_admin", "manual:bank-123"))
            self.assertTrue(self.user.get("/api/v1/points/me").json()["data"]["topup"]["manual"])
            self.assertFalse(self.user.get("/api/v1/points/topup").json()["data"]["open"])  # still no self-service channel

    def test_shop_spend_entitlement_and_refund(self):
        self.enable_2fa()
        self.adjust(150)
        with patch.dict(os.environ, {"AUU_POINTS_SHOP": "on"}):
            r = self.user.post("/api/v1/points/spend", json={"item": "nope", "idempotency_key": K})
            self.assertEqual(r.json()["error"]["code"], "POINTS_UNKNOWN_ITEM")
            r = self.user.post("/api/v1/points/spend", json={"item": "example_badge", "amount": 1}, headers={"Idempotency-Key": K})
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(r.json()["data"]["balance"], 50)  # price from the server catalog, client amount ignored
            spend_id = r.json()["data"]["entry"]["id"]
            again = self.user.post("/api/v1/points/spend", json={"item": "example_badge"}, headers={"Idempotency-Key": K})
            self.assertEqual((again.json()["data"]["replayed"], again.json()["data"]["balance"]), (True, 50))
            r = self.user.post("/api/v1/points/spend", json={"item": "example_badge", "idempotency_key": key("s2")})
            self.assertEqual(r.json()["error"]["code"], "POINTS_INSUFFICIENT")
            ent = self.user.get("/api/v1/points/me").json()["data"]["entitlements"]["example_badge"]
            self.assertTrue(ent["active"])
            refund = {"entry_id": spend_id, "amount": 101, "reason": "用户申请退款", "password": PW, "totp_code": self.code()}
            r = self.admin.post("/api/v1/admin/points/refund", json=refund, headers={"Idempotency-Key": key("r1")})
            self.assertEqual(r.json()["error"]["code"], "POINTS_BAD_AMOUNT")
            refund["amount"] = 100
            r = self.admin.post("/api/v1/admin/points/refund", json=refund, headers={"Idempotency-Key": key("r1")})
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(r.json()["data"]["balance"], 150)
            refund.update(amount=1, totp_code=self.code())
            r = self.admin.post("/api/v1/admin/points/refund", json=refund, headers={"Idempotency-Key": key("r2")})
            self.assertEqual(r.json()["error"]["code"], "POINTS_REFUND_EXCEEDS")
            self.assertFalse(self.user.get("/api/v1/points/me").json()["data"]["entitlements"]["example_badge"]["active"])
        self.assertTrue(self.admin.get("/api/v1/admin/points/verify").json()["data"]["ok"])

    def test_mock_topup_idempotent_and_failures(self):
        with patch.dict(os.environ, {"AUU_POINTS_MOCK": "on"}):
            st = self.user.get("/api/v1/points/topup").json()["data"]
            self.assertEqual((st["open"], st["channels"]), (True, ["mock"]))
            for bad in (0, -5, 1.5, "1e3", 100_001, True):
                r = self.user.post("/api/v1/points/topup", json={"provider": "mock", "points": bad, "idempotency_key": key("bad")})
                self.assertEqual(r.json()["error"]["code"], "POINTS_BAD_AMOUNT", bad)
            r = self.user.post("/api/v1/points/topup", json={"provider": "mock", "points": 120}, headers={"Idempotency-Key": key("t1")})
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual((r.json()["data"]["balance"], r.json()["data"]["order"]["status"]), (120, "paid"))
            r = self.user.post("/api/v1/points/topup", json={"provider": "mock", "points": 120}, headers={"Idempotency-Key": key("t1")})
            self.assertEqual((r.json()["data"]["replayed"], r.json()["data"]["balance"]), (True, 120))
            r = self.user.post("/api/v1/points/topup", json={"provider": "mock", "points": 121}, headers={"Idempotency-Key": key("t1")})
            self.assertEqual(r.status_code, 409)
            r = self.user.post("/api/v1/points/topup", json={"provider": "mock", "points": 13}, headers={"Idempotency-Key": key("t2")})
            self.assertEqual((r.json()["data"]["order"]["status"], r.json()["data"]["balance"]), ("failed", 120))
            r = self.user.post("/api/v1/points/topup", json={"provider": "mock", "points": 5})
            self.assertEqual(r.json()["error"]["code"], "POINTS_IDEMPOTENCY_REQUIRED")
            e = self.user.get("/api/v1/points/me").json()["data"]["entries"]
            self.assertEqual([(x["kind"], x["amount"], x["provider"]) for x in e], [("topup", 120, "mock")])


class SeparationTests(_Api):
    def test_points_never_touch_paper_books(self):
        from app.auth.accounts import journal_for, user_by_id

        book = journal_for(user_by_id(self.uid))
        before = (book.equity_0, len(book.closed), {k: len(v) for k, v in book.lots.items()})
        paper_files = {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file() and "points" not in p.name and p.name != "users.json"}
        self.enable_2fa()
        self.adjust(1000)
        self.adjust(-400)
        after = (book.equity_0, len(book.closed), {k: len(v) for k, v in book.lots.items()})
        self.assertEqual(before, after)
        for p, data in paper_files.items():
            self.assertEqual(p.read_bytes(), data, p)
        row = self.admin.get("/api/v1/auth/users").json()["data"]["items"]
        alice = [x for x in row if x["id"] == self.uid][0]
        self.assertEqual(alice["pnl"], 0)  # paper P&L unaffected by points
        self.assertTrue(str(os.environ["AUU_POINTS_STORE"]).endswith("points.sqlite"))

    def test_points_code_does_not_import_paper_or_strategy(self):
        root = Path(__file__).resolve().parents[1] / "app"
        for p in list((root / "points").glob("*.py")) + [root / "routes" / "points.py"]:
            text = p.read_text(encoding="utf-8")
            self.assertNotRegex(text, r"from app\.(paper|strategies|legacy|backtest|live)|import app\.(paper|strategies|legacy)", p.name)
        for p in list((root / "paper").glob("*.py")):
            self.assertNotIn("app.points", p.read_text(encoding="utf-8"), p.name)

    def test_catalog_items_change_no_existing_feature(self):
        self.assertEqual(list(catalog.ITEMS), ["example_badge"])
        app_root = Path(__file__).resolve().parents[1] / "app"
        users = [p for p in app_root.rglob("*.py") if "has_entitlement" in p.read_text(encoding="utf-8") and "points" not in p.parts]
        self.assertEqual(users, [])


if __name__ == "__main__":
    unittest.main()
