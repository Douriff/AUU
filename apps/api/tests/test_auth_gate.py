"""With AUU_AUTH=on the API is login-only; system-engine writes are admin-only."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from app.auth.accounts import reset_accounts
from app.auth.gate import gate_decision

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

PW = "gatePass123"

PRIVATE_GETS = [
    "/api/v1/strategy/pump-paper-v1",
    "/api/v1/strategy/pump-paper-v1/decision-log",
    "/api/v1/stats/paper-performance",
    "/api/v1/events",
    "/api/v1/board",
    "/api/v1/leaderboard",
    "/api/v1/watch/traders",
    "/api/v1/live/status",
    "/api/v1/markets",
    "/api/v1/symbols",
]

ADMIN_WRITES = [
    ("PUT", "/api/v1/strategy/pump-paper-v1"),
    ("PATCH", "/api/v1/strategy/pump-paper-v1"),
    ("POST", "/api/v1/strategy/pump-paper-v1"),
    ("POST", "/api/v1/strategy/pump-paper-v1/stats/reset"),
    ("POST", "/api/v1/strategy/pump-paper-v1/apply-distill"),
    ("PUT", "/api/v1/strategy/pump-paper-v1/shadow-compare"),
    ("PUT", "/api/v1/watch/traders"),
    ("DELETE", "/api/v1/watch/traders/watch-sniper"),
    ("POST", "/api/v1/paper/orders"),
    ("POST", "/api/v1/risk/pre-order"),
    ("POST", "/api/v1/pipeline/decide-and-fill"),
    ("PUT", "/api/v1/live/enabled"),
    ("PUT", "/api/v1/live/arm"),
    ("POST", "/api/v1/live/orders"),
]


class GateDecisionTests(unittest.TestCase):
    def test_rules(self):
        self.assertIsNone(gate_decision("/api/v1/health", "GET", None))
        self.assertIsNone(gate_decision("/api/v1/auth/login", "POST", None))
        self.assertIsNone(gate_decision("/", "GET", None))
        self.assertEqual(gate_decision("/api/v1/board", "GET", None)[0], 401)
        self.assertEqual(gate_decision("/api/v1/ws", "GET", None)[0], 401)
        self.assertEqual(gate_decision("//api/v1//strategy/pump-paper-v1/", "PUT", None)[0], 401)
        self.assertEqual(gate_decision("/api/v1/strategy/pump-paper-v1", "PUT", {"is_admin": False})[0], 403)
        self.assertIsNone(gate_decision("/api/v1/strategy/pump-paper-v1", "PUT", {"is_admin": True}))
        self.assertIsNone(gate_decision("/api/v1/strategy/pump-paper-v1", "GET", {"is_admin": False}))
        self.assertIsNone(gate_decision("/api/v1/trade/orders", "POST", {"is_admin": False}))
        self.assertIsNone(gate_decision("/api/v1/auth/users", "OPTIONS", None))


class AuthGateApiTests(unittest.TestCase):
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
        from app.strategies.pump_paper_v1 import get_engine

        self.TestClient = TestClient
        self.app = app
        self.engine = get_engine()
        self.auto_before = bool(self.engine.params.auto_paper_orders)

    def tearDown(self):
        for key, value in self._prev.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        reset_accounts()
        self.tmp.cleanup()

    def _signed_in(self, name):
        client = self.TestClient(self.app)
        body = {"name": name, "password": PW, "password_confirm": PW}
        res = client.post("/api/v1/auth/register", json=body)
        self.assertEqual(res.status_code, 200, res.text)
        return client

    def test_anonymous_gets_only_health_and_login_flow(self):
        anon = self.TestClient(self.app)
        self.assertEqual(anon.get("/api/v1/health").status_code, 200)
        me = anon.get("/api/v1/auth/me")
        self.assertEqual(me.status_code, 200)
        self.assertIsNone(me.json()["data"]["user"])
        for path in PRIVATE_GETS:
            with self.subTest(path=path):
                res = anon.get(path)
                self.assertEqual(res.status_code, 401, res.text)
                self.assertEqual(res.json()["error"]["code"], "AUTH_REQUIRED")

    def test_anonymous_writes_rejected_before_handler(self):
        anon = self.TestClient(self.app)
        for method, path in ADMIN_WRITES:
            with self.subTest(method=method, path=path):
                res = anon.request(method, path, json={"strategy_autopaper": not self.auto_before})
                self.assertEqual(res.status_code, 401, res.text)
        self.assertEqual(bool(self.engine.params.auto_paper_orders), self.auto_before)

    def test_non_admin_cannot_change_system_engine(self):
        self._signed_in("gateadmin")  # first account is admin
        user = self._signed_in("gateuser")
        self.assertEqual(user.get("/api/v1/strategy/pump-paper-v1").status_code, 200)
        for method, path in ADMIN_WRITES:
            with self.subTest(method=method, path=path):
                res = user.request(method, path, json={"strategy_autopaper": not self.auto_before})
                self.assertEqual(res.status_code, 403, res.text)
                self.assertEqual(res.json()["error"]["code"], "ADMIN_REQUIRED")
        self.assertEqual(bool(self.engine.params.auto_paper_orders), self.auto_before)

    def test_admin_can_toggle_autopaper(self):
        admin = self._signed_in("gateadmin")
        try:
            res = admin.put("/api/v1/strategy/pump-paper-v1", json={"strategy_autopaper": not self.auto_before})
            self.assertEqual(res.status_code, 200, res.text)
            self.assertEqual(bool(self.engine.params.auto_paper_orders), not self.auto_before)
        finally:
            self.engine.update_params({"auto_paper_orders": self.auto_before})

    def test_websocket_requires_session(self):
        from starlette.websockets import WebSocketDisconnect

        anon = self.TestClient(self.app)
        with self.assertRaises(WebSocketDisconnect) as ctx:
            with anon.websocket_connect("/api/v1/ws") as ws:
                ws.receive_json()
        self.assertEqual(ctx.exception.code, 1008)
        user = self._signed_in("gatews")
        with user.websocket_connect("/api/v1/ws") as ws:
            self.assertEqual(ws.receive_json()["type"], "hello")

    def test_auth_off_keeps_local_mode_open(self):
        os.environ["AUU_AUTH"] = "off"
        anon = self.TestClient(self.app)
        self.assertEqual(anon.get("/api/v1/strategy/pump-paper-v1").status_code, 200)


if __name__ == "__main__":
    unittest.main()
