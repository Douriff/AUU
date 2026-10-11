"""Shield: rate limits, scraper soft-ban, bad-UA block, trimmed public health, API headers."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.auth.accounts import reset_accounts
from app.security import shield
from app.security.shield import decide, reset_shield

UA = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148 Safari/604.1"
PW = "shieldPass123"


class DecideTests(unittest.TestCase):
    def setUp(self):
        reset_shield()

    def tearDown(self):
        reset_shield()

    def d(self, path="/api/v1/mainstream/overview", method="GET", query="", ip="203.0.113.7", user="u1", ua=UA, now=1000.0):
        return decide(path=path, method=method, query=query, ip=ip, user_id=user, ua=ua, now=now)

    def test_non_api_and_loopback_untouched(self):
        self.assertIsNone(self.d(path="/", ua=""))
        self.assertIsNone(self.d(path="/assets/x.js", ua="curl/8"))
        for ip in ("127.0.0.1", "::1", "127.0.0.53"):
            self.assertIsNone(self.d(ip=ip, ua="curl/8.0"))  # auu-guard / self-checks

    def test_bad_user_agents_blocked_except_health(self):
        for ua in ("", "python-requests/2.32", "curl/8.5.0", "Scrapy/2.11", "Go-http-client/1.1", "Java/17",
                   "Mozilla/5.0 (compatible; GPTBot/1.0)", "sqlmap/1.8"):
            with self.subTest(ua=ua):
                self.assertEqual(self.d(ua=ua)[:2], (403, "CLIENT_BLOCKED"))
        self.assertIsNone(self.d(path="/api/v1/health", ua="curl/8"))
        self.assertIsNone(self.d(path="/api/v1/status", ua=""))
        for ua in (UA, "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/129.0 Safari/537.36", "auu-failover/1"):
            self.assertIsNone(self.d(ua=ua))

    def test_polling_same_url_never_soft_bans(self):
        # Console polls every 2 s with a moving `since`; trading page polls the same URLs.
        for i in range(500):
            now = 1000.0 + i * 1.2
            self.assertIsNone(self.d(path="/api/v1/events", query=f"since={i}&limit=300", now=now), i)
            self.assertIsNone(self.d(path="/api/v1/mainstream/orderbook", query="symbol=BTC&depth=12", now=now))

    def test_per_user_and_per_ip_minute_limits(self):
        with mock.patch.dict(os.environ, {"AUU_RL_USER_PER_MIN": "50", "AUU_RL_IP_PER_MIN": "80"}):
            for i in range(50):
                self.assertIsNone(self.d(path="/api/v1/auth/me", now=1000 + i * 0.01))
            self.assertEqual(self.d(path="/api/v1/auth/me", now=1001)[:2], (429, "RATE_LIMIT"))
            # other account, same IP: allowed until the IP budget (80) runs out
            for i in range(29):
                self.assertIsNone(self.d(path="/api/v1/auth/me", user="u2", now=1001 + i * 0.01))
            self.assertEqual(self.d(path="/api/v1/auth/me", user="u3", now=1002)[:2], (429, "RATE_LIMIT"))
            # window slides
            self.assertIsNone(self.d(path="/api/v1/auth/me", user="u1", ip="198.51.100.9", now=1070))

    def test_write_limit_per_account(self):
        with mock.patch.dict(os.environ, {"AUU_RL_WRITE_PER_MIN": "5"}):
            for i in range(5):
                self.assertIsNone(self.d(path="/api/v1/mainstream/paper/orders", method="POST", now=1000 + i))
            self.assertEqual(self.d(path="/api/v1/mainstream/paper/orders", method="POST", now=1006)[0], 429)
            self.assertIsNone(self.d(path="/api/v1/mainstream/paper/orders", method="GET", now=1006))

    def test_bulk_crawl_soft_ban_then_expiry(self):
        with mock.patch.dict(os.environ, {"AUU_RL_DISTINCT_10MIN": "30", "AUU_RL_BAN_SEC": "120"}):
            for i in range(30):
                self.assertIsNone(self.d(path="/api/v1/mainstream/candles", query=f"symbol=BTC&tf=1h&before={i}", now=1000 + i))
            v = self.d(path="/api/v1/mainstream/candles", query="symbol=BTC&tf=1h&before=999", now=1031)
            self.assertEqual(v[:2], (429, "SCRAPE_SUSPECT"))
            self.assertEqual(v[3], 120)
            # banned for every endpoint while it lasts (account and IP)
            self.assertEqual(self.d(path="/api/v1/auth/me", now=1050)[1], "SCRAPE_SUSPECT")
            # same office IP, other account / anonymous: not affected (account-level ban only)
            self.assertIsNone(self.d(path="/api/v1/auth/me", user="u2", now=1050))
            self.assertIsNone(self.d(path="/api/v1/auth/me", user=None, now=1050))
            self.assertIsNone(self.d(user="u9", ip="198.51.100.1", now=1050))
            # soft: expires
            self.assertIsNone(self.d(path="/api/v1/auth/me", now=1152 + 600))

    def test_anonymous_crawl_bans_ip(self):
        with mock.patch.dict(os.environ, {"AUU_RL_DISTINCT_10MIN": "10"}):
            for i in range(10):
                self.assertIsNone(self.d(path="/api/v1/news", query=f"page={i}", user=None, now=1000 + i))
            self.assertEqual(self.d(path="/api/v1/news", query="page=99", user=None, now=1011)[1], "SCRAPE_SUSPECT")
            self.assertEqual(self.d(path="/api/v1/auth/me", user=None, now=1012)[1], "SCRAPE_SUSPECT")

    def test_url_key_ignores_since_and_order(self):
        self.assertEqual(shield._url_key("/a", "b=1&since=5&a=2"), shield._url_key("/a", "a=2&b=1&since=9"))


class ShieldApiTests(unittest.TestCase):
    _KEYS = ("AUU_AUTH", "AUU_ALLOW_SIGNUP", "AUU_INVITE_CODE", "AUU_USER_STORE", "AUU_USER_JOURNAL_DIR",
             "AUU_AUTH_RATE_MAX", "AUU_RL_DISTINCT_10MIN", "AUU_RL_USER_PER_MIN", "AUU_LEGACY_PUMP")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self._prev = {k: os.environ.get(k) for k in self._KEYS}
        os.environ.update({"AUU_AUTH": "on", "AUU_ALLOW_SIGNUP": "on", "AUU_USER_STORE": str(root / "users.json"),
                           "AUU_USER_JOURNAL_DIR": str(root / "journals"), "AUU_AUTH_RATE_MAX": "1000"})
        os.environ.pop("AUU_INVITE_CODE", None)
        reset_accounts()
        from fastapi.testclient import TestClient
        from app.main import create_app

        self.TestClient = TestClient
        self.app = create_app(legacy=False)

    def tearDown(self):
        for k, v in self._prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        reset_accounts()
        reset_shield()
        self.tmp.cleanup()

    def client(self, ip="203.0.113.7", ua=UA):
        return self.TestClient(self.app, client=(ip, 40000), headers={"User-Agent": ua})

    def signed_in(self, name="shield1", ip="203.0.113.7"):
        c = self.client(ip=ip)
        r = c.post("/api/v1/auth/register", json={"name": name, "password": PW, "password_confirm": PW})
        self.assertEqual(r.status_code, 200, r.text)
        return c

    def test_health_trimmed_for_anonymous_full_for_loopback_and_users(self):
        anon = self.client().get("/api/v1/health")
        self.assertEqual(anon.status_code, 200)
        h = anon.json()["data"]
        self.assertEqual(h["status"], "up")
        self.assertFalse(h["liveEnabled"])
        self.assertIn("role", h)
        for leak in ("strategyId", "runningStrategies", "mainstream", "recon", "alerts", "standby", "autopaperStall"):
            self.assertNotIn(leak, h)
        self.assertEqual(set(h.get("strategyRunner", {"active": 1})), {"active"})
        full = self.client(ip="127.0.0.1", ua="curl/8").get("/api/v1/health").json()["data"]
        self.assertIn("mainstream", full)
        self.assertIn("strategyRunner", full)
        user = self.signed_in().get("/api/v1/health").json()["data"]
        self.assertIn("mainstream", user)

    def test_spoofed_cf_connecting_ip_is_ignored(self):
        c = self.client(ip="203.0.113.50", ua="curl/8")
        r = c.get("/api/v1/mainstream/overview", headers={"CF-Connecting-IP": "127.0.0.1", "X-Forwarded-For": "127.0.0.1"})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()["error"]["code"], "CLIENT_BLOCKED")

    def test_data_endpoints_need_login(self):
        anon = self.client()
        for path in ("/api/v1/mainstream/overview", "/api/v1/mainstream/candles?symbol=BTC&tf=1d", "/api/v1/news",
                     "/api/v1/mainstream/strategy", "/api/v1/mainstream/shadow/h2", "/api/v1/points/me", "/api/v1/leaderboard"):
            with self.subTest(path=path):
                self.assertEqual(anon.get(path).status_code, 401)

    def test_api_security_headers(self):
        r = self.client().get("/api/v1/auth/me")
        self.assertEqual(r.headers["x-content-type-options"], "nosniff")
        self.assertEqual(r.headers["x-frame-options"], "DENY")
        self.assertEqual(r.headers["cross-origin-resource-policy"], "same-origin")
        self.assertIn("no-store", r.headers["cache-control"])

    def test_session_cookie_flags(self):
        with mock.patch.dict(os.environ, {"AUU_COOKIE_SECURE": "on"}):
            c = self.client()
            r = c.post("/api/v1/auth/register", json={"name": "cookie1", "password": PW, "password_confirm": PW})
        sc = r.headers["set-cookie"].lower()
        for flag in ("httponly", "secure", "samesite=lax", "path=/"):
            self.assertIn(flag, sc)

    def test_crawler_soft_banned_through_api(self):
        os.environ["AUU_RL_DISTINCT_10MIN"] = "15"
        c = self.signed_in("crawler1", ip="203.0.113.77")
        codes = [c.get(f"/api/v1/news?page={i}&size=20").status_code for i in range(1, 25)]
        self.assertIn(429, codes)
        self.assertNotIn(429, codes[:15])
        r = c.get("/api/v1/auth/me")
        self.assertEqual(r.status_code, 429)
        self.assertIn("retry-after", r.headers)
        # a normal user elsewhere still works
        other = self.signed_in("normal1", ip="198.51.100.20")
        self.assertEqual(other.get("/api/v1/news?page=1&size=20").status_code, 200)

    def test_csrf_cross_site_write_refused(self):
        c = self.signed_in("csrf1")
        r = c.post("/api/v1/mainstream/paper/orders", json={}, headers={"Origin": "https://evil.example"})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()["error"]["code"], "CSRF_ORIGIN")

    def test_password_spraying_from_one_ip_limited(self):
        with mock.patch.dict(os.environ, {"AUU_AUTH_RATE_MAX": "5"}):
            c = self.client(ip="203.0.113.99")
            codes = [c.post("/api/v1/auth/login", json={"name": f"victim{i}", "password": "wrongPass1"}).status_code
                     for i in range(8)]
        self.assertEqual(codes[:5], [401] * 5)
        self.assertEqual(codes[5:], [429] * 3)

    def test_shield_off_switch(self):
        with mock.patch.dict(os.environ, {"AUU_SHIELD": "off"}):
            self.assertEqual(self.client(ua="curl/8").get("/api/v1/auth/me").status_code, 200)


if __name__ == "__main__":
    unittest.main()
