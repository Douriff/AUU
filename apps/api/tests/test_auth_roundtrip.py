"""Signup then login must work for any valid password, and the password rule matches the web form."""
from __future__ import annotations

import json
import os
import re
import tempfile
import unittest
from pathlib import Path

from app.auth.accounts import password_problem, reset_accounts

SPECIAL = "Abc123!@#$%^&*()_+-=[]{};':\",./<>?\\|"
_KEYS = (
    "AUU_AUTH",
    "AUU_ALLOW_SIGNUP",
    "AUU_INVITE_CODE",
    "AUU_ADMIN_USER",
    "AUU_USER_STORE",
    "AUU_USER_JOURNAL_DIR",
    "AUU_AUTH_RATE_MAX",
    "DATA_PROVIDER",
    "PUMP_PAPER_LOOP",
    "PUMPFUN_DISCOVERY",
)


class AuthRoundtripTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self._prev = {key: os.environ.get(key) for key in _KEYS}
        os.environ["AUU_AUTH"] = "on"
        os.environ["AUU_ALLOW_SIGNUP"] = "on"
        os.environ.pop("AUU_INVITE_CODE", None)
        os.environ.pop("AUU_ADMIN_USER", None)
        os.environ["AUU_USER_STORE"] = str(root / "users.json")
        os.environ["AUU_USER_JOURNAL_DIR"] = str(root / "journals")
        os.environ["AUU_AUTH_RATE_MAX"] = "1000"
        os.environ["DATA_PROVIDER"] = "mock"
        os.environ["PUMP_PAPER_LOOP"] = "0"
        os.environ["PUMPFUN_DISCOVERY"] = "off"
        reset_accounts()
        from fastapi.testclient import TestClient
        from app.main import app

        self.app = app
        self.TestClient = TestClient
        self.store = root / "users.json"

    def tearDown(self):
        for key, value in self._prev.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        reset_accounts()
        self.tmp.cleanup()

    def _client(self):
        return self.TestClient(self.app)

    def _register(self, name, password, client=None, **extra):
        body = {"name": name, "password": password, "password_confirm": password, **extra}
        return (client or self._client()).post("/api/v1/auth/register", json=body)

    def _login(self, name, password, client=None):
        return (client or self._client()).post("/api/v1/auth/login", json={"name": name, "password": password})

    def test_signup_then_login_with_special_and_unicode_passwords(self):
        cases = [
            ("special", SPECIAL),
            ("unicode", "密码Pässwörd9"),
            ("emoji", "abc12345🙂"),
            ("spaces", " a1 b2 c3 "),
            ("backslash", "a\\1\\\"'`~"),
        ]
        for name, password in cases:
            with self.subTest(name=name):
                created = self._register(name, password)
                self.assertEqual(created.status_code, 200, created.text)
                cookie = created.headers.get("set-cookie", "")
                self.assertIn("auu_session=", cookie)
                self.assertIn("HttpOnly", cookie)
                self.assertIn("samesite=lax", cookie.lower())
                self.assertNotIn("secure", cookie.lower())
                self.assertNotIn("domain=", cookie.lower())
                fresh = self._client()
                logged = self._login(name, password, fresh)
                self.assertEqual(logged.status_code, 200, logged.text)
                self.assertEqual(logged.json()["data"]["user"]["name"], name)
                me = fresh.get("/api/v1/auth/me").json()["data"]
                self.assertEqual(me["user"]["name"], name)
                self.assertFalse(me["liveEnabled"])
                wrong = self._login(name, password + "x")
                self.assertEqual(wrong.status_code, 401)
                self.assertNotIn(password, created.text + logged.text)
        stored = json.loads(self.store.read_text(encoding="utf-8"))
        blob = json.dumps(stored, ensure_ascii=False)
        self.assertNotIn(SPECIAL, blob)
        self.assertTrue(all(row["password_hash"].startswith("$2") for row in stored["users"]))
        self.assertTrue(stored["users"][0]["is_admin"])
        self.assertFalse(any(row["is_admin"] for row in stored["users"][1:]))

    def test_raw_json_body_with_special_characters_roundtrips(self):
        body = json.dumps({"name": "rawjson", "password": SPECIAL, "password_confirm": SPECIAL}, ensure_ascii=True)
        created = self._client().post("/api/v1/auth/register", content=body, headers={"content-type": "application/json"})
        self.assertEqual(created.status_code, 200, created.text)
        body = json.dumps({"name": "rawjson", "password": SPECIAL}, ensure_ascii=False).encode("utf-8")
        logged = self._client().post("/api/v1/auth/login", content=body, headers={"content-type": "application/json; charset=utf-8"})
        self.assertEqual(logged.status_code, 200, logged.text)

    def test_username_case_and_whitespace(self):
        self.assertEqual(self._register("  Alice_1 ", "password1").status_code, 200)
        for variant in ("alice_1", "ALICE_1", "  Alice_1  ", "\tAlice_1\n"):
            with self.subTest(variant=variant):
                logged = self._login(variant, "password1")
                self.assertEqual(logged.status_code, 200, logged.text)
                self.assertEqual(logged.json()["data"]["user"]["name"], "Alice_1")
        taken = self._register("ALICE_1", "password1")
        self.assertEqual(taken.json()["error"]["code"], "NAME_TAKEN")
        self.assertEqual(self._login("alice_1", " password1").status_code, 401)

    def test_usernames_with_common_punctuation_are_accepted(self):
        for name in ("zhang-san", "a.b", "me@example.com", "张三", "x_1"):
            with self.subTest(name=name):
                created = self._register(name, "password1")
                self.assertEqual(created.status_code, 200, created.text)
                self.assertEqual(self._login(name.upper(), "password1").status_code, 200)
        for name in ("x", "-lead", ".lead", "has space", "a/b", "a" * 33, "<script>"):
            with self.subTest(bad=name):
                bad = self._register(name, "password1")
                self.assertEqual(bad.status_code, 400)
                self.assertEqual(bad.json()["error"]["code"], "BAD_NAME")
                self.assertIn("用户名", bad.json()["error"]["message"])

    def test_password_rule_accepts_and_rejects(self):
        good = ["password1", "12345678a", SPECIAL, "Ab1!!!!!", "密码密码密码12", "Pässwört1", "a" * 70 + "12"]
        bad = ["", "short1a", "abcdefgh", "12345678", "!@#$%^&*()", "密码密码密码密码", "１２３４５６ab", "a1" + "密" * 24]
        for password in good:
            with self.subTest(good=password):
                self.assertIsNone(password_problem(password))
        for password in bad:
            with self.subTest(bad=password):
                self.assertEqual(password_problem(password), "BAD_PASSWORD")
        for index, password in enumerate(bad):
            with self.subTest(api_bad=password):
                rejected = self._register(f"bad{index}", password)
                self.assertEqual(rejected.status_code, 400)
                error = rejected.json()["error"]
                self.assertEqual(error["code"], "BAD_PASSWORD")
                self.assertIn("字母和数字", error["message"])
                self.assertIn("特殊字符", error["message"])
                if password:
                    self.assertNotIn(password, rejected.text)
        self.assertFalse(self.store.exists() and json.loads(self.store.read_text())["users"])

    def test_password_change_uses_the_same_rule(self):
        client = self._client()
        self.assertEqual(self._register("changer", "password1", client).status_code, 200)
        weak = client.post(
            "/api/v1/auth/password",
            json={"current_password": "password1", "new_password": "abcdefgh", "new_password_confirm": "abcdefgh"},
        )
        self.assertEqual(weak.json()["error"]["code"], "BAD_PASSWORD")
        strong = client.post(
            "/api/v1/auth/password",
            json={"current_password": "password1", "new_password": SPECIAL, "new_password_confirm": SPECIAL},
        )
        self.assertEqual(strong.status_code, 200, strong.text)
        self.assertEqual(self._login("changer", SPECIAL).status_code, 200)
        self.assertEqual(self._login("changer", "password1").status_code, 401)

    def test_web_form_uses_the_same_rules_and_messages(self):
        from app.routes.auth import _MESSAGES

        web = Path(__file__).resolve().parents[2] / "web" / "src" / "lib" / "authRules.ts"
        source = web.read_text(encoding="utf-8")
        # Rule texts live in the i18n catalog now; the Chinese (default) wording must equal the server's.
        zh = json.loads((web.parents[1] / "i18n" / "locales" / "zh-CN.json").read_text(encoding="utf-8"))["auth"]["rule"]
        for key, code in (("password", "BAD_PASSWORD"), ("name", "BAD_NAME"), ("display", "BAD_DISPLAY"), ("start", "BAD_START")):
            self.assertEqual(zh[key], _MESSAGES[code], key)
            self.assertIn(f'i18n.t("auth.rule.{key}")', source)
        from app.auth import accounts

        pattern = re.search(r"const NAME = /(.+)/;", source).group(1)
        self.assertEqual(pattern, accounts._NAME.pattern)

    def test_blank_start_sol_uses_default(self):
        created = self._register("blankstart", "password1", start_sol="")
        self.assertEqual(created.status_code, 200, created.text)
        zero = self._register("zerostart", "password1", start_sol=0)
        self.assertEqual(zero.json()["error"]["code"], "BAD_START")


if __name__ == "__main__":
    unittest.main()
