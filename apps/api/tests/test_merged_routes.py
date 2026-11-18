"""The recovery branch keeps the screener and AI lab routes, and their admin gates."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from app.screener.access import decide

PW = "password1"


class MergedRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self._prev = {key: os.environ.get(key) for key in ("AUU_AUTH", "AUU_ALLOW_SIGNUP", "AUU_USER_STORE", "AUU_USER_JOURNAL_DIR", "AUU_AUTH_RATE_MAX", "AUU_SESSION_SECRET", "AUU_INVITE_CODE", "AUU_ADMIN_USER")}
        os.environ.update({
            "AUU_AUTH": "on", "AUU_ALLOW_SIGNUP": "on", "AUU_AUTH_RATE_MAX": "1000",
            "AUU_USER_STORE": str(root / "users.json"), "AUU_USER_JOURNAL_DIR": str(root / "journals"),
            "AUU_SESSION_SECRET": "merged-route-secret",
        })
        os.environ.pop("AUU_INVITE_CODE", None)
        os.environ.pop("AUU_ADMIN_USER", None)
        from app.auth.accounts import reset_accounts
        from app.main import create_app
        from fastapi.testclient import TestClient

        reset_accounts()
        self.app = create_app(legacy=False)
        self.TC = TestClient
        self.paths = set(self.app.openapi()["paths"])

    def tearDown(self):
        for key, value in self._prev.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        from app.auth.accounts import reset_accounts
        reset_accounts()
        self.tmp.cleanup()

    def test_screener_ai_and_resume_are_all_mounted(self):
        self.assertIn("/api/v1/strategy/resume", self.paths)
        self.assertIn("/api/v1/screener/spot", self.paths)
        self.assertIn("/api/v1/ai-review/plaza", self.paths)
        self.assertIn("/api/v1/ai-review/relay", self.paths)

    def test_guest_is_refused_and_admin_can_open_each_surface(self):
        guest = self.TC(self.app)
        admin = self.TC(self.app)
        self.assertEqual(admin.post("/api/v1/auth/register", json={"name": "owner", "password": PW, "password_confirm": PW}).status_code, 200)
        self.assertEqual(guest.post("/api/v1/auth/register", json={"name": "guest", "password": PW, "password_confirm": PW}).status_code, 200)
        self.assertEqual(guest.post("/api/v1/strategy/resume", json={"confirm": "resume-forward"}).status_code, 403)
        self.assertEqual(guest.get("/api/v1/screener/spot").status_code, 403)
        self.assertEqual(guest.get("/api/v1/ai-review/plaza").status_code, 403)
        self.assertEqual(guest.get("/api/v1/ai-review/relay").status_code, 403)
        opened = admin.post("/api/v1/strategy/resume", json={"confirm": "nope"})
        self.assertEqual(opened.status_code, 400)
        self.assertEqual(opened.json()["error"]["code"], "CONFIRM")
        self.assertEqual(admin.get("/api/v1/screener/spot").status_code, 200)
        plaza = admin.get("/api/v1/ai-review/plaza")
        self.assertNotIn(plaza.status_code, (401, 403, 404), plaza.text[:300])
        relay = admin.get("/api/v1/ai-review/relay")
        self.assertNotIn(relay.status_code, (401, 403, 404), relay.text[:300])

    def test_admin_predicates_ignore_client_flags(self):
        self.assertEqual(decide({"is_admin": False, "subscribed": True}, auth_on=True)[0], 403)
        self.assertIsNone(decide({"is_admin": True}, auth_on=True))
        self.assertEqual(decide(None, auth_on=True)[0], 401)
