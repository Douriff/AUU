"""Optional TOTP 2FA, recovery codes, login records, sign out other sessions."""
from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path

import pyotp

from app.auth.accounts import reset_accounts
from app.auth.security import match_step, recovery_hash

_KEYS = ("AUU_AUTH", "AUU_ALLOW_SIGNUP", "AUU_INVITE_CODE", "AUU_ADMIN_USER", "AUU_USER_STORE", "AUU_USER_JOURNAL_DIR",
         "AUU_AUTH_RATE_MAX", "DATA_PROVIDER", "PUMP_PAPER_LOOP", "PUMPFUN_DISCOVERY")
PW = "password123"
UA = {"user-agent": "UnitTest/1.0"}


class TwoFactorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self._prev = {k: os.environ.get(k) for k in _KEYS}
        os.environ.update({"AUU_AUTH": "on", "AUU_ALLOW_SIGNUP": "on", "AUU_USER_STORE": str(root / "users.json"),
                           "AUU_USER_JOURNAL_DIR": str(root / "journals"), "AUU_AUTH_RATE_MAX": "1000",
                           "DATA_PROVIDER": "mock", "PUMP_PAPER_LOOP": "0", "PUMPFUN_DISCOVERY": "off"})
        os.environ.pop("AUU_INVITE_CODE", None)
        os.environ.pop("AUU_ADMIN_USER", None)
        reset_accounts()
        from fastapi.testclient import TestClient
        from app.main import app

        self.TC, self.app, self.store = TestClient, app, root / "users.json"
        self.c = TestClient(app, headers=UA)
        r = self.c.post("/api/v1/auth/register", json={"name": "wangshu", "password": PW, "password_confirm": PW})
        self.assertEqual(r.status_code, 200)

    def tearDown(self):
        for k, v in self._prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        reset_accounts()
        self.tmp.cleanup()

    def _login(self, client=None, pw=PW):
        return (client or self.TC(self.app, headers=UA)).post("/api/v1/auth/login", json={"name": "wangshu", "password": pw})

    def _enable(self):
        s = self.c.post("/api/v1/auth/2fa/setup", json={"password": PW})
        self.assertEqual(s.status_code, 200, s.text)
        secret = s.json()["data"]["secret"]
        self.assertIn("<svg", s.json()["data"]["qr_svg"])
        self.assertTrue(s.json()["data"]["otpauth"].startswith("otpauth://totp/AUUTRADE"))
        e = self.c.post("/api/v1/auth/2fa/enable", json={"code": pyotp.TOTP(secret).now()})
        self.assertEqual(e.status_code, 200, e.text)
        return secret, e.json()["data"]["recovery_codes"]

    def _next_code(self, secret):
        """A code for a step not used yet (the replay guard refuses the step used to enable)."""
        return pyotp.TOTP(secret).at(time.time() + 30)

    def test_default_off_login_unchanged_and_recorded(self):
        r = self._login()
        self.assertEqual(r.status_code, 200)
        self.assertIn("auu_session", r.cookies)
        self.assertNotIn("totp_required", r.json()["data"])
        self.assertFalse(r.json()["data"]["user"]["totp_enabled"])
        self._login(pw="wrongpass1")
        sec = self.c.get("/api/v1/auth/security").json()["data"]
        self.assertFalse(sec["totp_enabled"])
        results = [x["result"] for x in sec["logins"]]
        self.assertEqual(results[:2], ["bad_password", "ok"])
        self.assertEqual(sec["logins"][1]["ua"], "UnitTest/1.0")
        self.assertTrue(sec["logins"][1]["ip"])

    def test_enable_requires_password_and_a_valid_code(self):
        self.assertEqual(self.c.post("/api/v1/auth/2fa/setup", json={"password": "nope12345"}).status_code, 401)
        s = self.c.post("/api/v1/auth/2fa/setup", json={"password": PW}).json()["data"]
        self.assertEqual(self.c.post("/api/v1/auth/2fa/enable", json={"code": "000000"}).status_code, 401)
        self.assertFalse(self.c.get("/api/v1/auth/security").json()["data"]["totp_enabled"])
        self.assertEqual(self.c.post("/api/v1/auth/2fa/enable", json={"code": pyotp.TOTP(s["secret"]).now()}).status_code, 200)

    def test_secret_encrypted_and_recovery_hashed_at_rest(self):
        secret, codes = self._enable()
        self.assertEqual(len(codes), 10)
        raw = self.store.read_text()
        self.assertNotIn(secret, raw)
        for c in codes:
            self.assertNotIn(c, raw)
            self.assertNotIn(c.replace("-", ""), raw)
        t = json.loads(raw)["users"][0]["totp"]
        self.assertIn(recovery_hash(codes[0]), t["recovery"])
        me = self.c.get("/api/v1/auth/me").text
        self.assertNotIn("secret_enc", me)
        self.assertNotIn("recovery", me)

    def test_login_needs_second_step(self):
        secret, codes = self._enable()
        r = self._login()
        d = r.json()["data"]
        self.assertTrue(d["totp_required"])
        self.assertNotIn("auu_session", r.cookies)
        self.assertNotIn("user", d)
        c2 = self.TC(self.app, headers=UA)
        self.assertEqual(c2.get("/api/v1/auth/security").status_code, 401)
        self.assertEqual(c2.post("/api/v1/auth/login/totp", json={"ticket": d["ticket"], "code": "123456"}).status_code, 401)
        self.assertEqual(c2.post("/api/v1/auth/login/totp", json={"ticket": "x.y.z.w", "code": "123456"}).status_code, 401)
        ok = c2.post("/api/v1/auth/login/totp", json={"ticket": d["ticket"], "code": self._next_code(secret)})
        self.assertEqual(ok.status_code, 200, ok.text)
        self.assertEqual(c2.get("/api/v1/auth/security").status_code, 200)
        # the same step cannot be replayed
        d2 = self._login().json()["data"]
        again = self.TC(self.app).post("/api/v1/auth/login/totp", json={"ticket": d2["ticket"], "code": self._next_code(secret)})
        self.assertEqual(again.status_code, 401)

    def test_recovery_code_works_once(self):
        _, codes = self._enable()
        for expect in (200, 401):
            d = self._login().json()["data"]
            r = self.TC(self.app).post("/api/v1/auth/login/totp", json={"ticket": d["ticket"], "code": codes[0].upper()})
            self.assertEqual(r.status_code, expect)
        self.assertEqual(self.c.get("/api/v1/auth/security").json()["data"]["recovery_left"], 9)

    def test_disable_and_password_change_need_reverification(self):
        secret, codes = self._enable()
        self.assertEqual(self.c.post("/api/v1/auth/2fa/disable", json={"password": PW}).status_code, 400)  # TOTP_REQUIRED
        self.assertEqual(self.c.post("/api/v1/auth/2fa/disable", json={"password": PW, "code": "000000"}).status_code, 401)
        r = self.c.post("/api/v1/auth/password", json={"current_password": PW, "new_password": "newpass456", "new_password_confirm": "newpass456"})
        self.assertEqual(r.json()["error"]["code"], "TOTP_REQUIRED")
        r = self.c.post("/api/v1/auth/password", json={"current_password": PW, "new_password": "newpass456",
                                                        "new_password_confirm": "newpass456", "totp_code": codes[1]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.c.get("/api/v1/auth/security").status_code, 200)  # this device re-stamped
        r = self.c.post("/api/v1/auth/2fa/disable", json={"password": "newpass456", "code": self._next_code(secret)})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertNotIn("totp", json.loads(self.store.read_text())["users"][0])
        r = self._login(pw="newpass456")
        self.assertIn("auu_session", r.cookies)

    def test_revoke_other_sessions(self):
        other = self.TC(self.app, headers=UA)
        self.assertEqual(self._login(other).status_code, 200)
        self.assertEqual(other.get("/api/v1/auth/security").status_code, 200)
        r = self.c.post("/api/v1/auth/sessions/revoke-others")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(other.get("/api/v1/auth/security").status_code, 401)
        self.assertEqual(self.c.get("/api/v1/auth/security").status_code, 200)
        self.assertIn("revoked_other_sessions", [x["result"] for x in self.c.get("/api/v1/auth/security").json()["data"]["logins"]])

    def test_new_recovery_codes_invalidate_old(self):
        secret, codes = self._enable()
        r = self.c.post("/api/v1/auth/2fa/recovery", json={"password": PW, "code": self._next_code(secret)})
        self.assertEqual(r.status_code, 200, r.text)
        new = r.json()["data"]["recovery_codes"]
        d = self._login().json()["data"]
        self.assertEqual(self.TC(self.app).post("/api/v1/auth/login/totp", json={"ticket": d["ticket"], "code": codes[5]}).status_code, 401)
        self.assertEqual(self.TC(self.app).post("/api/v1/auth/login/totp", json={"ticket": d["ticket"], "code": new[0]}).status_code, 200)

    def test_match_step_drift_window(self):
        s = pyotp.random_base32()
        t = 1_800_000_000
        self.assertIsNotNone(match_step(s, pyotp.TOTP(s).at(t - 30), now=t))
        self.assertIsNone(match_step(s, pyotp.TOTP(s).at(t - 90), now=t))
        self.assertIsNone(match_step(s, pyotp.TOTP(s).at(t), now=t, last_step=t // 30))


if __name__ == "__main__":
    unittest.main()
