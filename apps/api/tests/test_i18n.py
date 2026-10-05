"""Interface languages: account locale endpoint, localized verification emails, web locale parity."""
from __future__ import annotations

import json
import os
import re
import tempfile
import unittest
from pathlib import Path

from app.auth import email_codes
from app.auth.accounts import LOCALES, reset_accounts
from app.auth.email_i18n import EMAIL_LANGS, TEXTS, email_lang

_KEYS = (
    "AUU_AUTH",
    "AUU_ALLOW_SIGNUP",
    "AUU_INVITE_CODE",
    "AUU_ADMIN_USER",
    "AUU_USER_STORE",
    "AUU_USER_JOURNAL_DIR",
    "AUU_AUTH_RATE_MAX",
    "AUU_EMAIL_VERIFY",
    "AUU_SMTP_USER",
    "AUU_SMTP_PASS",
    "DATA_PROVIDER",
    "PUMP_PAPER_LOOP",
    "PUMPFUN_DISCOVERY",
)
PW = "Abc12345xyz"
HAN = re.compile(r"[\u3400-\u9fff]")
WEB_LOCALES = Path(__file__).resolve().parents[2] / "web" / "src" / "i18n" / "locales"


class _Env(unittest.TestCase):
    extra: dict[str, str] = {}

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
                "DATA_PROVIDER": "mock",
                "PUMP_PAPER_LOOP": "0",
                "PUMPFUN_DISCOVERY": "off",
                **self.extra,
            }
        )
        self.store = root / "users.json"
        reset_accounts()
        from fastapi.testclient import TestClient
        from app.main import app

        self.TestClient = TestClient
        self.app = app
        self.client = TestClient(app)

    def tearDown(self):
        for key, value in self._prev.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        reset_accounts()
        self.tmp.cleanup()


class LocaleEndpointTests(_Env):
    def _register(self, name="lena"):
        body = {"name": name, "password": PW, "password_confirm": PW}
        res = self.client.post("/api/v1/auth/register", json=body)
        self.assertEqual(res.status_code, 200, res.text)

    def test_requires_login(self):
        res = self.client.put("/api/v1/auth/locale", json={"locale": "de"})
        self.assertEqual(res.status_code, 401, res.text)
        self.assertEqual(res.json()["error"]["code"], "AUTH_REQUIRED")

    def test_set_and_read_back(self):
        self._register()
        me = self.client.get("/api/v1/auth/me").json()["data"]
        self.assertIsNone(me["user"]["locale"])
        res = self.client.put("/api/v1/auth/locale", json={"locale": "de"})
        self.assertEqual(res.status_code, 200, res.text)
        data = res.json()["data"]
        self.assertEqual(data["locale"], "de")
        self.assertGreater(data["locale_ts"], 0)
        self.assertFalse(data["liveEnabled"])
        me = self.client.get("/api/v1/auth/me").json()["data"]
        self.assertEqual(me["user"]["locale"], "de")
        stored = json.loads(self.store.read_text(encoding="utf-8"))["users"][0]
        self.assertEqual(stored["locale"], "de")
        # Another device logging in sees the account language.
        other = self.TestClient(self.app)
        self.assertEqual(other.post("/api/v1/auth/login", json={"name": "lena", "password": PW}).status_code, 200)
        self.assertEqual(other.get("/api/v1/auth/me").json()["data"]["user"]["locale"], "de")

    def test_every_supported_locale(self):
        self._register()
        for loc in LOCALES:
            res = self.client.put("/api/v1/auth/locale", json={"locale": loc})
            self.assertEqual(res.status_code, 200, (loc, res.text))
            self.assertEqual(res.json()["data"]["locale"], loc)

    def test_rejects_unknown_locale(self):
        self._register()
        for bad in ("xx", "", "zh-TW", "<script>", 5):
            res = self.client.put("/api/v1/auth/locale", json={"locale": bad})
            self.assertEqual(res.status_code, 400, (bad, res.text))
            self.assertEqual(res.json()["error"]["code"], "BAD_LOCALE")
        self.assertIsNone(self.client.get("/api/v1/auth/me").json()["data"]["user"]["locale"])

    def test_locale_not_on_leaderboard(self):
        self._register()
        self.client.put("/api/v1/auth/locale", json={"locale": "ja"})
        board = self.client.get("/api/v1/leaderboard")
        if board.status_code == 200:
            self.assertNotIn('"locale"', board.text)


class FakeSender:
    def __init__(self):
        self.sent: list[tuple[str, str, str]] = []

    def __call__(self, to, subject, body):
        self.sent.append((to, subject, body))


class LocalizedEmailRouteTests(_Env):
    extra = {"AUU_EMAIL_VERIFY": "on", "AUU_SMTP_USER": "sender@qq.com", "AUU_SMTP_PASS": "fake-pass"}

    def setUp(self):
        super().setUp()
        self.mail = FakeSender()
        email_codes.set_sender(self.mail)

    def tearDown(self):
        email_codes.set_sender(None)
        super().tearDown()

    def _send(self, email, lang=None):
        body = {"email": email, "purpose": "signup"}
        if lang is not None:
            body["lang"] = lang
        res = self.client.post("/api/v1/auth/email/code", json=body)
        self.assertEqual(res.status_code, 200, res.text)
        return self.mail.sent[-1]

    def test_default_is_chinese(self):
        _to, subject, body = self._send("a@qq.com")
        self.assertIn("注册验证码", subject)
        self.assertIn("验证码为：", body)

    def test_english_and_german(self):
        _to, subject, body = self._send("b@qq.com", "en")
        self.assertIn("verification code", subject)
        self.assertIsNone(HAN.search(subject + body))
        code = re.search(r"\b(\d{6})\b", body).group(1)
        self.assertIn(code, subject)
        _to, subject, body = self._send("c@qq.com", "de-AT")
        self.assertIn("Bestätigungscode", subject + body)
        self.assertIsNone(HAN.search(body))

    def test_unknown_lang_falls_back_to_chinese(self):
        _to, subject, _body = self._send("d@qq.com", "xx-YY")
        self.assertIn("注册验证码", subject)


class EmailRenderTests(unittest.TestCase):
    def test_all_languages_render(self):
        self.assertEqual(set(EMAIL_LANGS), set(LOCALES))
        keys = set(TEXTS["zh-CN"])
        for lang in EMAIL_LANGS:
            self.assertEqual(set(TEXTS[lang]), keys, lang)
            for purpose in ("signup", "reset"):
                subject, text, html_body = email_codes.render_email(purpose, "123456", lang)
                self.assertIn("123456", subject, (lang, purpose))
                self.assertIn("123456", text)
                self.assertIn("AUUTRADE", text + subject)
                self.assertNotIn("{", subject + text, (lang, purpose))
                self.assertIn(f'lang="{lang}"', html_body)
                self.assertIn('dir="rtl"' if lang == "ar" else 'dir="ltr"', html_body.split("\n", 2)[1])
                self.assertIn('<span dir="ltr"', html_body)
                if lang not in ("zh-CN", "ja", "ko"):
                    self.assertIsNone(HAN.search(subject + text), (lang, purpose))

    def test_email_lang_mapping(self):
        self.assertEqual(email_lang("zh-TW"), "zh-CN")
        self.assertEqual(email_lang("pt-BR"), "pt")
        self.assertEqual(email_lang("EN"), "en")
        self.assertEqual(email_lang(None), "zh-CN")
        self.assertEqual(email_lang("klingon"), "zh-CN")


def _flat(obj, pre=""):
    out = {}
    for key, value in obj.items():
        full = f"{pre}.{key}" if pre else key
        if isinstance(value, dict):
            out.update(_flat(value, full))
        else:
            out[full] = str(value)
    return out


def _sig(text):
    return sorted(re.sub(r"\s", "", m) for m in re.findall(r"\{\{\s*\w+\s*\}\}|</?\w+>", text))


@unittest.skipUnless(WEB_LOCALES.is_dir(), "web locales not present")
class WebLocaleParityTests(unittest.TestCase):
    def test_locale_files_match_chinese_keys(self):
        files = {p.stem for p in WEB_LOCALES.glob("*.json")}
        self.assertEqual(files, set(LOCALES))
        zh = _flat(json.loads((WEB_LOCALES / "zh-CN.json").read_text(encoding="utf-8")))
        self.assertGreater(len(zh), 500)
        for lang in LOCALES:
            if lang == "zh-CN":
                continue
            m = _flat(json.loads((WEB_LOCALES / f"{lang}.json").read_text(encoding="utf-8")))
            self.assertEqual(set(m), set(zh), lang)
            same = 0
            for key, ref in zh.items():
                val = m[key]
                self.assertTrue(val.strip(), (lang, key))
                self.assertEqual(_sig(val), _sig(ref), (lang, key))
                if lang not in ("ja", "ko"):
                    self.assertIsNone(HAN.search(val), (lang, key, val))
                if HAN.search(ref) and val == ref:
                    same += 1
            self.assertLess(same, len(zh) * 0.05, lang)

    def test_brand_and_tickers_untranslated(self):
        for lang in LOCALES:
            m = _flat(json.loads((WEB_LOCALES / f"{lang}.json").read_text(encoding="utf-8")))
            self.assertIn("AUUTRADE", m["nav.homeAria"], lang)
            self.assertIn("BTC", m["ml.noMatchHint"], lang)
            self.assertIn("USDT", m["trade.amountUsdt"], lang)
