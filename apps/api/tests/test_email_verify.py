"""Email verification codes at signup and password reset. A fake sender only; never real SMTP."""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.auth import email_codes
from app.auth.accounts import reset_accounts

_KEYS = (
    "AUU_AUTH",
    "AUU_ALLOW_SIGNUP",
    "AUU_INVITE_CODE",
    "AUU_ADMIN_USER",
    "AUU_USER_STORE",
    "AUU_USER_JOURNAL_DIR",
    "AUU_AUTH_RATE_MAX",
    "AUU_EMAIL_VERIFY",
    "AUU_SMTP_HOST",
    "AUU_SMTP_PORT",
    "AUU_SMTP_USER",
    "AUU_SMTP_PASS",
    "AUU_SMTP_STARTTLS",
    "AUU_EMAIL_IP_MAX",
    "AUU_EMAIL_IP_WINDOW",
    "DATA_PROVIDER",
    "PUMP_PAPER_LOOP",
    "PUMPFUN_DISCOVERY",
)
SMTP_PASS = "fake-auth-code-xyz"
PW = "Abc123!@#$%^&*()"


class FakeSender:
    def __init__(self):
        self.sent: list[tuple[str, str, str]] = []
        self.fail = False

    def __call__(self, to, subject, body):
        if self.fail:
            raise OSError("boom to " + to)
        self.sent.append((to, subject, body))

    def last_code(self, to=None):
        for addr, _subject, body in reversed(self.sent):
            if to is None or addr == to:
                return re.search(r"验证码：(\d{6})", body).group(1)
        raise AssertionError("no mail")


class EmailVerifyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self._prev = {key: os.environ.get(key) for key in _KEYS}
        for key in _KEYS:
            os.environ.pop(key, None)
        os.environ.update(
            {
                "AUU_AUTH": "on",
                "AUU_ALLOW_SIGNUP": "on",
                "AUU_USER_STORE": str(root / "users.json"),
                "AUU_USER_JOURNAL_DIR": str(root / "journals"),
                "AUU_AUTH_RATE_MAX": "1000",
                "AUU_EMAIL_VERIFY": "on",
                "AUU_SMTP_USER": "sender@qq.com",
                "AUU_SMTP_PASS": SMTP_PASS,
                "DATA_PROVIDER": "mock",
                "PUMP_PAPER_LOOP": "0",
                "PUMPFUN_DISCOVERY": "off",
            }
        )
        self.store = root / "users.json"
        reset_accounts()
        self.mail = FakeSender()
        email_codes.set_sender(self.mail)
        self.clock = [1_000_000.0]
        patcher = mock.patch.object(email_codes, "_now", side_effect=lambda: self.clock[0])
        patcher.start()
        self.addCleanup(patcher.stop)
        from fastapi.testclient import TestClient
        from app.main import app

        self.client = TestClient(app)
        self.TestClient = TestClient
        self.app = app

    def tearDown(self):
        email_codes.set_sender(None)
        for key, value in self._prev.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        reset_accounts()
        self.tmp.cleanup()

    def _code(self, email, purpose="signup", client=None):
        return (client or self.client).post("/api/v1/auth/email/code", json={"email": email, "purpose": purpose})

    def _register(self, name, email, code, password=PW, client=None):
        body = {"name": name, "password": password, "password_confirm": password, "email": email, "email_code": code}
        return (client or self.client).post("/api/v1/auth/register", json=body)

    def test_send_and_register_with_code(self):
        me = self.client.get("/api/v1/auth/me").json()["data"]
        self.assertTrue(me["email_verify"])
        sent = self._code("  Alice@QQ.com ")
        self.assertEqual(sent.status_code, 200, sent.text)
        data = sent.json()["data"]
        self.assertEqual(data["email"], "a***@qq.com")
        self.assertEqual(len(self.mail.sent), 1)
        to, subject, body = self.mail.sent[0]
        self.assertEqual(to, "alice@qq.com")
        self.assertIn("验证码", subject)
        self.assertIn("10 分钟", body)
        code = self.mail.last_code()
        self.assertNotIn(code, sent.text)
        created = self._register("alice", "ALICE@qq.com", code)
        self.assertEqual(created.status_code, 200, created.text)
        self.assertNotIn("alice@qq.com", created.text)
        stored = json.loads(self.store.read_text(encoding="utf-8"))["users"][0]
        self.assertEqual(stored["email"], "alice@qq.com")
        blob = self.store.read_text(encoding="utf-8")
        self.assertNotIn(code, blob.replace(stored["id"], "").replace(stored["password_hash"], ""))
        self.assertEqual(self.TestClient(self.app).post("/api/v1/auth/login", json={"name": "alice", "password": PW}).status_code, 200)
        reused = self._register("alice2", "alice@qq.com", code)
        self.assertEqual(reused.json()["error"]["code"], "EMAIL_TAKEN")

    def test_register_requires_email_and_valid_code(self):
        missing = self._register("bob", "", "")
        self.assertEqual(missing.json()["error"]["code"], "EMAIL_REQUIRED")
        bad = self._register("bob", "not-an-email", "123456")
        self.assertEqual(bad.json()["error"]["code"], "BAD_EMAIL")
        nocode = self._register("bob", "bob@163.com", "")
        self.assertEqual(nocode.json()["error"]["code"], "EMAIL_CODE_REQUIRED")
        never = self._register("bob", "bob@163.com", "123456")
        self.assertEqual(never.json()["error"]["code"], "EMAIL_CODE_EXPIRED")
        self.assertIn("重新获取", never.json()["error"]["message"])
        self.assertFalse(self.store.exists())

    def test_code_is_hashed_in_memory(self):
        self._code("carl@qq.com")
        code = self.mail.last_code()
        rows = list(email_codes._CODES.values())
        self.assertEqual(len(rows), 1)
        self.assertNotIn(code, json.dumps(rows))

    def test_expiry_after_ten_minutes(self):
        self._code("dora@qq.com")
        code = self.mail.last_code()
        self.clock[0] += 601
        late = self._register("dora", "dora@qq.com", code)
        self.assertEqual(late.json()["error"]["code"], "EMAIL_CODE_EXPIRED")
        self._code("dora@qq.com")
        fresh = self.mail.last_code()
        self.clock[0] += 599
        self.assertEqual(self._register("dora", "dora@qq.com", fresh).status_code, 200)

    def test_resend_throttle_and_new_code_replaces_old(self):
        self.assertEqual(self._code("eve@qq.com").status_code, 200)
        first = self.mail.last_code()
        again = self._code("EVE@qq.com")
        self.assertEqual(again.status_code, 429)
        self.assertEqual(again.json()["error"]["code"], "EMAIL_THROTTLE")
        self.assertRegex(again.json()["error"]["message"], r"请 \d+ 秒后再试")
        self.assertEqual(len(self.mail.sent), 1)
        self.clock[0] += 61
        self.assertEqual(self._code("eve@qq.com").status_code, 200)
        second = self.mail.last_code()
        if second != first:
            self.assertEqual(self._register("eve", "eve@qq.com", first).json()["error"]["code"], "EMAIL_CODE_BAD")
        self.assertEqual(self._register("eve", "eve@qq.com", second).status_code, 200)

    def test_per_ip_cap(self):
        os.environ["AUU_EMAIL_IP_MAX"] = "3"
        for index in range(3):
            self.assertEqual(self._code(f"user{index}@qq.com").status_code, 200)
        capped = self._code("user9@qq.com")
        self.assertEqual(capped.status_code, 429)
        self.assertEqual(capped.json()["error"]["code"], "EMAIL_IP_LIMIT")
        self.assertEqual(len(self.mail.sent), 3)
        self.clock[0] += 3601
        self.assertEqual(self._code("user9@qq.com").status_code, 200)

    def test_five_wrong_attempts_lock_the_code(self):
        self._code("finn@qq.com")
        code = self.mail.last_code()
        wrong = "000000" if code != "000000" else "111111"
        codes = [self._register("finn", "finn@qq.com", wrong).json()["error"]["code"] for _ in range(5)]
        self.assertEqual(codes, ["EMAIL_CODE_BAD"] * 4 + ["EMAIL_CODE_LOCKED"])
        locked = self._register("finn", "finn@qq.com", code)
        self.assertEqual(locked.json()["error"]["code"], "EMAIL_CODE_LOCKED")
        self.clock[0] += 61
        self._code("finn@qq.com")
        self.assertEqual(self._register("finn", "finn@qq.com", self.mail.last_code()).status_code, 200)

    def test_validation_errors_do_not_burn_the_code(self):
        self._code("gina@qq.com")
        code = self.mail.last_code()
        weak = self._register("gina", "gina@qq.com", code, password="abcdefgh")
        self.assertEqual(weak.json()["error"]["code"], "BAD_PASSWORD")
        badname = self._register("-x", "gina@qq.com", code)
        self.assertEqual(badname.json()["error"]["code"], "BAD_NAME")
        self.assertEqual(self._register("gina", "gina@qq.com", code).status_code, 200)

    def test_email_unique_case_insensitive(self):
        self._code("Hank@QQ.com")
        self.assertEqual(self._register("hank", "hank@qq.com", self.mail.last_code()).status_code, 200)
        taken = self._code("HANK@qq.COM")
        self.assertEqual(taken.json()["error"]["code"], "EMAIL_TAKEN")
        self.assertIn("忘记密码", taken.json()["error"]["message"])
        self.assertEqual(len(self.mail.sent), 1)

    def test_send_failure_is_reported_and_retryable(self):
        self.mail.fail = True
        with self.assertLogs("auu.email", level="WARNING") as logs:
            failed = self._code("ivan@qq.com")
        self.assertEqual(failed.status_code, 502)
        self.assertEqual(failed.json()["error"]["code"], "EMAIL_SEND_FAILED")
        joined = "\n".join(logs.output)
        self.assertNotIn("ivan@qq.com", joined)
        self.assertIn("i***@qq.com", joined)
        self.mail.fail = False
        self.assertEqual(self._code("ivan@qq.com").status_code, 200)

    def test_logs_never_contain_secrets(self):
        with self.assertLogs("auu.email", level="INFO") as logs:
            self._code("june@qq.com")
        code = self.mail.last_code()
        joined = "\n".join(logs.output)
        for secret in (code, "june@qq.com", SMTP_PASS):
            self.assertNotIn(secret, joined)
        me = self.client.get("/api/v1/auth/me").text
        self.assertNotIn(SMTP_PASS, me)
        self.assertNotIn("sender@qq.com", me)

    def test_not_configured_and_off_mode_endpoints(self):
        os.environ.pop("AUU_SMTP_PASS")
        missing = self._code("kate@qq.com")
        self.assertEqual(missing.status_code, 503)
        self.assertEqual(missing.json()["error"]["code"], "EMAIL_NOT_CONFIGURED")
        os.environ["AUU_EMAIL_VERIFY"] = "off"
        self.assertEqual(self._code("kate@qq.com").json()["error"]["code"], "EMAIL_OFF")
        self.assertFalse(self.client.get("/api/v1/auth/me").json()["data"]["email_verify"])
        reset = self.client.post(
            "/api/v1/auth/password/reset",
            json={"email": "kate@qq.com", "code": "123456", "new_password": PW, "new_password_confirm": PW},
        )
        self.assertEqual(reset.json()["error"]["code"], "EMAIL_OFF")
        self.assertEqual(self.mail.sent, [])

    def test_off_mode_registers_without_email(self):
        os.environ["AUU_EMAIL_VERIFY"] = "off"
        created = self.client.post(
            "/api/v1/auth/register", json={"name": "lena", "password": "password1", "password_confirm": "password1"}
        )
        self.assertEqual(created.status_code, 200, created.text)
        ignored = self.client.post(
            "/api/v1/auth/register",
            json={"name": "lena2", "password": "password1", "password_confirm": "password1", "email": "x", "email_code": "1"},
        )
        self.assertEqual(ignored.status_code, 200, ignored.text)
        self.assertEqual(self.mail.sent, [])

    def _signup(self, name, email, client):
        self._code(email, client=client)
        created = self._register(name, email, self.mail.last_code(email), client=client)
        self.assertEqual(created.status_code, 200, created.text)
        self.clock[0] += 61

    def test_password_reset_flow_invalidates_sessions(self):
        browser = self.TestClient(self.app)
        self._signup("mike", "mike@163.com", browser)
        other = self.TestClient(self.app)
        self.assertEqual(other.post("/api/v1/auth/login", json={"name": "mike", "password": PW}).status_code, 200)
        self.assertEqual(browser.get("/api/v1/auth/me").json()["data"]["user"]["name"], "mike")
        sent = self._code("MIKE@163.com", purpose="reset")
        self.assertEqual(sent.status_code, 200)
        self.assertIn("如果该邮箱已注册", sent.json()["data"]["message"])
        code = self.mail.last_code("mike@163.com")
        self.assertIn("重置密码", self.mail.sent[-1][1])
        new = "N3w-pass!word"
        mismatch = self.client.post(
            "/api/v1/auth/password/reset", json={"email": "mike@163.com", "code": code, "new_password": new, "new_password_confirm": "x"}
        )
        self.assertEqual(mismatch.json()["error"]["code"], "PASSWORD_MISMATCH")
        weak = self.client.post(
            "/api/v1/auth/password/reset",
            json={"email": "mike@163.com", "code": code, "new_password": "abcdefgh", "new_password_confirm": "abcdefgh"},
        )
        self.assertEqual(weak.json()["error"]["code"], "BAD_PASSWORD")
        done = self.client.post(
            "/api/v1/auth/password/reset", json={"email": "mike@163.com", "code": code, "new_password": new, "new_password_confirm": new}
        )
        self.assertEqual(done.status_code, 200, done.text)
        self.assertIn("密码已重置", done.json()["data"]["message"])
        self.assertIsNone(browser.get("/api/v1/auth/me").json()["data"]["user"])
        self.assertIsNone(other.get("/api/v1/auth/me").json()["data"]["user"])
        self.assertEqual(self.TestClient(self.app).post("/api/v1/auth/login", json={"name": "mike", "password": PW}).status_code, 401)
        fresh = self.TestClient(self.app)
        self.assertEqual(fresh.post("/api/v1/auth/login", json={"name": "mike", "password": new}).status_code, 200)
        self.assertEqual(fresh.get("/api/v1/auth/me").json()["data"]["user"]["name"], "mike")
        replay = self.client.post(
            "/api/v1/auth/password/reset", json={"email": "mike@163.com", "code": code, "new_password": PW, "new_password_confirm": PW}
        )
        self.assertEqual(replay.json()["error"]["code"], "EMAIL_CODE_EXPIRED")

    def test_reset_for_unknown_email_sends_nothing_and_looks_the_same(self):
        sent = self._code("ghost@qq.com", purpose="reset")
        self.assertEqual(sent.status_code, 200)
        self.assertIn("如果该邮箱已注册", sent.json()["data"]["message"])
        self.assertEqual(self.mail.sent, [])
        guess = self.client.post(
            "/api/v1/auth/password/reset", json={"email": "ghost@qq.com", "code": "123456", "new_password": PW, "new_password_confirm": PW}
        )
        self.assertEqual(guess.json()["error"]["code"], "EMAIL_CODE_EXPIRED")

    def test_reset_wrong_code_lockout(self):
        self._signup("nora", "nora@qq.com", self.TestClient(self.app))
        self._code("nora@qq.com", purpose="reset")
        code = self.mail.last_code()
        wrong = "000000" if code != "000000" else "111111"
        body = {"email": "nora@qq.com", "code": wrong, "new_password": "Zz9zz9zz9", "new_password_confirm": "Zz9zz9zz9"}
        codes = [self.client.post("/api/v1/auth/password/reset", json=body).json()["error"]["code"] for _ in range(5)]
        self.assertEqual(codes[-1], "EMAIL_CODE_LOCKED")
        body["code"] = code
        self.assertEqual(self.client.post("/api/v1/auth/password/reset", json=body).json()["error"]["code"], "EMAIL_CODE_LOCKED")
        self.assertEqual(self.client.post("/api/v1/auth/login", json={"name": "nora", "password": PW}).status_code, 200)

    def test_pre_epoch_sessions_stay_valid(self):
        from app.auth.accounts import issue_token, user_from_token

        browser = self.TestClient(self.app)
        self._signup("olga", "olga@qq.com", browser)
        user_id = browser.get("/api/v1/auth/me").json()["data"]["user"]["id"]
        token = issue_token(user_id)
        self.assertEqual(user_from_token(token)["id"], user_id)
        self.assertIsNone(user_from_token(token[:-1] + ("0" if token[-1] != "0" else "1")))


class WebParityTests(unittest.TestCase):
    def test_web_email_rule_matches_api(self):
        from app.routes.auth import _MESSAGES

        web = Path(__file__).resolve().parents[2] / "web" / "src" / "lib" / "authRules.ts"
        source = web.read_text(encoding="utf-8")
        self.assertIn(f'EMAIL_RULE = "{_MESSAGES["BAD_EMAIL"]}"', source)
        pattern = re.search(r"const EMAIL = /(.+)/;", source).group(1)
        self.assertEqual(pattern, email_codes._EMAIL.pattern)


class SmtpSenderTests(unittest.TestCase):
    def setUp(self):
        self._prev = {key: os.environ.get(key) for key in _KEYS}
        for key in ("AUU_SMTP_HOST", "AUU_SMTP_PORT", "AUU_SMTP_STARTTLS"):
            os.environ.pop(key, None)
        os.environ["AUU_SMTP_USER"] = "sender@163.com"
        os.environ["AUU_SMTP_PASS"] = SMTP_PASS

    def tearDown(self):
        for key, value in self._prev.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_defaults_to_qq_ssl_465(self):
        cfg = email_codes.smtp_settings()
        self.assertEqual((cfg["host"], cfg["port"]), ("smtp.qq.com", 465))

    def test_ssl_path_logs_in_and_sends_without_network(self):
        with mock.patch.object(email_codes.smtplib, "SMTP_SSL") as ssl_cls, mock.patch.object(email_codes.smtplib, "SMTP") as plain:
            email_codes._smtp_send("to@qq.com", "主题", "正文 123456")
        ssl_cls.assert_called_once()
        self.assertEqual(ssl_cls.call_args.args[:2], ("smtp.qq.com", 465))
        client = ssl_cls.return_value.__enter__.return_value
        client.login.assert_called_once_with("sender@163.com", SMTP_PASS)
        msg = client.send_message.call_args.args[0]
        self.assertEqual(msg["To"], "to@qq.com")
        self.assertIn("sender@163.com", msg["From"])
        plain.assert_not_called()

    def test_starttls_path_for_587(self):
        os.environ["AUU_SMTP_HOST"] = "smtp.163.com"
        os.environ["AUU_SMTP_PORT"] = "587"
        with mock.patch.object(email_codes.smtplib, "SMTP") as plain, mock.patch.object(email_codes.smtplib, "SMTP_SSL") as ssl_cls:
            email_codes._smtp_send("to@qq.com", "s", "b")
        client = plain.return_value.__enter__.return_value
        client.starttls.assert_called_once()
        client.login.assert_called_once()
        ssl_cls.assert_not_called()

    def test_gmail_presets_and_app_password_spaces(self):
        os.environ["AUU_SMTP_HOST"] = "smtp.gmail.com"
        os.environ["AUU_SMTP_USER"] = "someone@gmail.com"
        os.environ["AUU_SMTP_PASS"] = " abcd efgh\tijkl mnop "
        for port, starttls in (("465", False), ("587", True)):
            os.environ["AUU_SMTP_PORT"] = port
            cfg = email_codes.smtp_settings()
            self.assertEqual((cfg["host"], cfg["port"], cfg["starttls"]), ("smtp.gmail.com", int(port), starttls))
            self.assertEqual(cfg["password"], "abcdefghijklmnop")
        os.environ["AUU_SMTP_PORT"] = "587"
        with mock.patch.object(email_codes.smtplib, "SMTP") as plain, mock.patch.object(email_codes.smtplib, "SMTP_SSL") as ssl_cls:
            email_codes._smtp_send("to@qq.com", "s", "b")
        self.assertEqual(plain.call_args.args[:2], ("smtp.gmail.com", 587))
        client = plain.return_value.__enter__.return_value
        client.starttls.assert_called_once()
        client.login.assert_called_once_with("someone@gmail.com", "abcdefghijklmnop")
        self.assertIn("someone@gmail.com", client.send_message.call_args.args[0]["From"])
        ssl_cls.assert_not_called()
        os.environ["AUU_SMTP_PORT"] = "465"
        with mock.patch.object(email_codes.smtplib, "SMTP_SSL") as ssl_cls, mock.patch.object(email_codes.smtplib, "SMTP") as plain:
            email_codes._smtp_send("to@qq.com", "s", "b")
        self.assertEqual(ssl_cls.call_args.args[:2], ("smtp.gmail.com", 465))
        ssl_cls.return_value.__enter__.return_value.login.assert_called_once_with("someone@gmail.com", "abcdefghijklmnop")
        plain.assert_not_called()

    def test_password_strips_invisible_and_format_characters(self):
        pasted = (
            "\ufeff\u200babcd\u00a0efgh\u200c\u200d\u2060ijkl\u3000mnop"
            "\u2028\u2029\u202f\u2009\u00ad\u200e\u200f\r\n"
        )
        os.environ["AUU_SMTP_HOST"] = "smtp.gmail.com"
        os.environ["AUU_SMTP_USER"] = "someone@gmail.com"
        os.environ["AUU_SMTP_PASS"] = pasted
        self.assertEqual(email_codes.smtp_settings()["password"], "abcdefghijklmnop")
        self.assertTrue(email_codes.smtp_configured())
        with mock.patch.object(email_codes.smtplib, "SMTP_SSL") as ssl_cls, mock.patch.object(email_codes.smtplib, "SMTP"):
            email_codes._smtp_send("to@qq.com", "s", "b")
        ssl_cls.return_value.__enter__.return_value.login.assert_called_once_with("someone@gmail.com", "abcdefghijklmnop")

    def test_clean_password_keeps_real_characters(self):
        clean = email_codes.clean_smtp_password
        self.assertEqual(clean("Ab1!@#$%^&*()-_=+[]{};:'\",.<>/?`~|\\"), "Ab1!@#$%^&*()-_=+[]{};:'\",.<>/?`~|\\")
        self.assertEqual(clean("授权码ÄÖü"), "授权码ÄÖü")
        self.assertEqual(clean(""), "")
        self.assertEqual(clean(None), "")
        self.assertEqual(clean("\u200b \ufeff\u00a0"), "")

    def test_invisible_only_password_is_not_configured(self):
        os.environ["AUU_SMTP_PASS"] = "\u200b\ufeff\u00a0 \u2060"
        self.assertEqual(email_codes.smtp_settings()["password"], "")
        self.assertFalse(email_codes.smtp_configured())

    def test_starttls_flag_overrides_port(self):
        os.environ["AUU_SMTP_PORT"] = "2525"
        os.environ["AUU_SMTP_STARTTLS"] = "off"
        self.assertFalse(email_codes.smtp_settings()["starttls"])
        os.environ["AUU_SMTP_PORT"] = "465"
        os.environ["AUU_SMTP_STARTTLS"] = "on"
        self.assertTrue(email_codes.smtp_settings()["starttls"])
        os.environ.pop("AUU_SMTP_STARTTLS")
        self.assertFalse(email_codes.smtp_settings()["starttls"])

    def test_mask(self):
        self.assertEqual(email_codes.mask_email("alice@qq.com"), "a***@qq.com")
        self.assertEqual(email_codes.mask_email("bad"), "***")


if __name__ == "__main__":
    unittest.main()
