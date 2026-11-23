"""Logout revokes one device. Other devices stay until revoke-others or a password change."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from app.auth.accounts import issue_token, public_signup_open, reset_accounts, user_from_token
from tests.auth_fixtures import seed_admin

PW = "password1"
_KEYS = (
    "AUU_AUTH", "AUU_ALLOW_SIGNUP", "AUU_INVITE_CODE", "AUU_ADMIN_USER", "AUU_USER_STORE",
    "AUU_USER_JOURNAL_DIR", "AUU_AUTH_RATE_MAX", "AUU_SESSION_SECRET", "DATA_PROVIDER",
    "PUMP_PAPER_LOOP", "PUMPFUN_DISCOVERY",
)


class SessionReplayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self._prev = {key: os.environ.get(key) for key in _KEYS}
        os.environ.update({
            "AUU_AUTH": "on", "AUU_ALLOW_SIGNUP": "on", "AUU_AUTH_RATE_MAX": "1000",
            "AUU_USER_STORE": str(root / "users.json"), "AUU_USER_JOURNAL_DIR": str(root / "journals"),
            "AUU_SESSION_SECRET": "probe-session-secret", "DATA_PROVIDER": "mock",
            "PUMP_PAPER_LOOP": "0", "PUMPFUN_DISCOVERY": "off",
        })
        os.environ.pop("AUU_INVITE_CODE", None)
        os.environ.pop("AUU_ADMIN_USER", None)
        reset_accounts()
        from fastapi.testclient import TestClient
        from app.main import app

        self.app = app
        self.TC = TestClient

    def tearDown(self):
        for key, value in self._prev.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        reset_accounts()
        self.tmp.cleanup()

    def _reg(self, client, name):
        if not public_signup_open():
            seed_admin(name, PW)
            res = client.post("/api/v1/auth/login", json={"name": name, "password": PW})
        else:
            res = client.post("/api/v1/auth/register", json={"name": name, "password": PW, "password_confirm": PW})
        self.assertEqual(res.status_code, 200, res.text)
        return res

    def test_logout_cannot_be_replayed_and_other_device_stays(self):
        admin = self.TC(self.app)
        other = self.TC(self.app)
        guest = self.TC(self.app)
        self._reg(admin, "owner")
        self.assertTrue(admin.get("/api/v1/auth/me").json()["data"]["user"]["is_admin"])
        self.assertEqual(other.post("/api/v1/auth/login", json={"name": "owner", "password": PW}).status_code, 200)
        self._reg(guest, "guest")
        self.assertFalse(guest.get("/api/v1/auth/me").json()["data"]["user"]["is_admin"])
        self.assertEqual(guest.get("/api/v1/auth/users").status_code, 403)
        stolen = admin.cookies.get("auu_session")
        self.assertEqual(admin.post("/api/v1/auth/logout").status_code, 200)
        self.assertIn("其他设备", admin.post("/api/v1/auth/logout").json()["data"]["message"])
        replay = self.TC(self.app)
        replay.cookies.set("auu_session", stolen)
        self.assertIsNone(replay.get("/api/v1/auth/me").json()["data"]["user"])
        self.assertEqual(replay.get("/api/v1/auth/users").status_code, 401)
        self.assertEqual(other.get("/api/v1/auth/users").status_code, 200)
        self.assertEqual({row["name"] for row in other.get("/api/v1/auth/users").json()["data"]["items"]}, {"owner", "guest"})
        self.assertEqual(other.get("/api/v1/auth/me").json()["data"]["devices"]["count"], 1)

    def test_revoke_others_and_password_change_have_explicit_scope(self):
        here = self.TC(self.app)
        there = self.TC(self.app)
        self._reg(here, "owner")
        self.assertEqual(there.post("/api/v1/auth/login", json={"name": "owner", "password": PW}).status_code, 200)
        old = there.cookies.get("auu_session")
        res = here.post("/api/v1/auth/sessions/revoke-others")
        self.assertEqual(res.status_code, 200, res.text)
        self.assertIn("本机保持登录", res.json()["data"]["message"])
        self.assertEqual(here.get("/api/v1/auth/me").status_code, 200)
        self.assertIsNotNone(here.get("/api/v1/auth/me").json()["data"]["user"])
        replay = self.TC(self.app)
        replay.cookies.set("auu_session", old)
        self.assertEqual(replay.get("/api/v1/auth/users").status_code, 401)
        there.cookies.set("auu_session", old)
        self.assertEqual(there.get("/api/v1/auth/users").status_code, 401)
        self.assertEqual(there.post("/api/v1/auth/login", json={"name": "owner", "password": PW}).status_code, 200)
        changed = here.post("/api/v1/auth/password", json={
            "current_password": PW, "new_password": "newpass456", "new_password_confirm": "newpass456",
        })
        self.assertEqual(changed.status_code, 200, changed.text)
        self.assertIsNotNone(here.get("/api/v1/auth/me").json()["data"]["user"])
        self.assertEqual(there.get("/api/v1/auth/users").status_code, 401)

    def test_old_token_shape_and_dropped_device_are_rejected(self):
        client = self.TC(self.app)
        user_id = self._reg(client, "owner").json()["data"]["user"]["id"]
        self.assertIsNone(user_from_token(f"{user_id}.9999999999.not-a-device-signature"))
        first = issue_token(user_id)
        extra = [issue_token(user_id) for _ in range(8)]
        self.assertIsNone(user_from_token(first))
        self.assertEqual(user_from_token(extra[-1])["id"], user_id)
