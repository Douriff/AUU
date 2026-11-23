"""Isolated public register -> login flow, and the closed-signup path.

Public HTTP never grants admin. The first admin is created with bootstrap_admin
so a later public signup is not automatically privileged.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from app.auth.accounts import reset_accounts
from tests.auth_fixtures import seed_admin


_KEYS = (
    "AUU_AUTH",
    "AUU_ALLOW_SIGNUP",
    "AUU_USER_STORE",
    "AUU_USER_JOURNAL_DIR",
    "AUU_AUTH_RATE_MAX",
    "AUU_INVITE_CODE",
    "AUU_ADMIN_USER",
    "AUU_EMAIL_VERIFY",
    "DATA_PROVIDER",
    "PUMP_PAPER_LOOP",
    "PUMPFUN_DISCOVERY",
)


class RegisterPublicFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.store = root / "users.json"
        self._prev = {key: os.environ.get(key) for key in _KEYS}
        os.environ["AUU_AUTH"] = "on"
        os.environ["AUU_ALLOW_SIGNUP"] = "on"
        os.environ["AUU_AUTH_RATE_MAX"] = "1000"
        os.environ["AUU_USER_STORE"] = str(self.store)
        os.environ["AUU_USER_JOURNAL_DIR"] = str(root / "journals")
        os.environ["AUU_ADMIN_USER"] = "owner"
        os.environ["AUU_EMAIL_VERIFY"] = "off"
        os.environ["DATA_PROVIDER"] = "pumpfun_paper"
        os.environ["PUMP_PAPER_LOOP"] = "0"
        os.environ["PUMPFUN_DISCOVERY"] = "off"
        os.environ.pop("AUU_INVITE_CODE", None)
        reset_accounts()
        reserved = seed_admin("owner", "password1")
        self.assertTrue(reserved["is_admin"])
        from fastapi.testclient import TestClient
        from app.main import app

        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        reset_accounts()
        for key, value in self._prev.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp.cleanup()

    def test_signup_on_registers_logs_in_and_is_not_admin_or_live(self):
        me = self.client.get("/api/v1/auth/me")
        self.assertEqual(me.status_code, 200)
        body = me.json()["data"]
        self.assertTrue(body["auth_enabled"])
        self.assertTrue(body["signup_allowed"])
        self.assertFalse(body.get("email_verify"))
        self.assertIsNone(body.get("user"))

        created = self.client.post(
            "/api/v1/auth/register",
            json={"name": "paperuser", "password": "password1", "password_confirm": "password1"},
        )
        self.assertEqual(created.status_code, 200, created.text)
        payload = created.json()["data"]
        self.assertEqual(payload["user"]["name"], "paperuser")
        self.assertFalse(payload["user"]["is_admin"])
        self.assertFalse(payload["liveEnabled"])

        who = self.client.get("/api/v1/auth/me").json()["data"]["user"]
        self.assertEqual(who["name"], "paperuser")
        self.assertFalse(who["is_admin"])

        self.assertEqual(self.client.post("/api/v1/auth/logout").status_code, 200)
        self.assertIsNone(self.client.get("/api/v1/auth/me").json()["data"]["user"])

        logged = self.client.post(
            "/api/v1/auth/login",
            json={"name": "paperuser", "password": "password1"},
        )
        self.assertEqual(logged.status_code, 200, logged.text)
        self.assertEqual(logged.json()["data"]["user"]["name"], "paperuser")
        self.assertFalse(logged.json()["data"]["user"]["is_admin"])
        self.assertFalse(logged.json()["data"].get("liveEnabled"))

    def test_signup_off_rejects_register_and_creates_no_user(self):
        os.environ["AUU_ALLOW_SIGNUP"] = "off"
        me = self.client.get("/api/v1/auth/me").json()["data"]
        self.assertFalse(me["signup_allowed"])
        closed = self.client.post(
            "/api/v1/auth/register",
            json={"name": "sneaky", "password": "password1", "password_confirm": "password1"},
        )
        self.assertEqual(closed.status_code, 400, closed.text)
        self.assertEqual(closed.json()["error"]["code"], "SIGNUP_CLOSED")
        self.assertIsNone(self.client.get("/api/v1/auth/me").json()["data"]["user"])
        bad = self.client.post("/api/v1/auth/login", json={"name": "sneaky", "password": "password1"})
        self.assertEqual(bad.status_code, 401)
        owner = self.client.post("/api/v1/auth/login", json={"name": "owner", "password": "password1"})
        self.assertEqual(owner.status_code, 200, owner.text)
