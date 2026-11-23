"""With AUU_AUTH=on the API is login-only; system-engine writes are admin-only."""
from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from unittest.mock import patch
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
    "/api/v1/mainstream/strategy",
    "/api/v1/mainstream/paper/account",
    "/api/v1/mainstream/signal",
    "/api/v1/mainstream/shadow/h2",
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
        self.assertEqual(gate_decision("//api/v1//strategy/pump-paper-v1/", "PUT", None)[0], 401)
        self.assertEqual(gate_decision("/api/v1/strategy/pump-paper-v1", "PUT", {"is_admin": False})[0], 403)
        self.assertIsNone(gate_decision("/api/v1/strategy/pump-paper-v1", "PUT", {"is_admin": True}))
        self.assertIsNone(gate_decision("/api/v1/strategy/pump-paper-v1", "GET", {"is_admin": False}))
        self.assertIsNone(gate_decision("/api/v1/trade/orders", "POST", {"is_admin": False}))
        self.assertIsNone(gate_decision("/api/v1/auth/users", "OPTIONS", None))
        self.assertEqual(gate_decision("/api/v1/status", "GET", None)[0], 401)
        self.assertEqual(gate_decision("/api/v1/status", "GET", {"is_admin": False})[0], 403)
        self.assertIsNone(gate_decision("/api/v1/status", "GET", {"is_admin": True}))
        for path in (
            "/api/v1/mainstream/markets",
            "/api/v1/mainstream/overview",
            "/api/v1/mainstream/candles",
            "/api/v1/mainstream/orderbook",
            "/api/v1/mainstream/coin",
            "/api/v1/mainstream/funding",
            "/api/v1/majors",
            "/api/v1/majors/tickers",
            "/api/v1/ws",
        ):
            self.assertIsNone(gate_decision(path, "GET", None), path)
        self.assertEqual(gate_decision("/api/v1/mainstream/candles", "POST", None)[0], 401)
        self.assertEqual(gate_decision("/api/v1/mainstream/paper/orders", "POST", None)[0], 401)
        self.assertEqual(gate_decision("/api/v1/exchange/accounts", "GET", None)[0], 401)
        self.assertEqual(gate_decision("/api/v1/live/orders", "POST", None)[0], 401)


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
        from app.legacy.pump.strategies.pump_paper_v1 import get_engine

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

    def test_websocket_market_hello_is_public_and_omits_private_events(self):
        anon = self.TestClient(self.app)
        with anon.websocket_connect("/api/v1/ws") as ws:
            hello = ws.receive_json()
        self.assertEqual(hello["type"], "hello")
        self.assertNotIn("fill", hello["eventTypes"])
        user = self._signed_in("gatews")
        with user.websocket_connect("/api/v1/ws") as ws:
            self.assertEqual(ws.receive_json()["type"], "hello")

    def test_auth_off_keeps_local_mode_open(self):
        os.environ["AUU_AUTH"] = "off"
        anon = self.TestClient(self.app)
        self.assertEqual(anon.get("/api/v1/strategy/pump-paper-v1").status_code, 200)


if __name__ == "__main__":
    unittest.main()


class SecurityHardeningTests(AuthGateApiTests):
    def test_cross_site_writes_and_ws_refused(self):
        admin = self._signed_in("csrfadmin")
        evil = {"Origin": "https://evil.example"}
        res = admin.put("/api/v1/strategy/pump-paper-v1", json={"strategy_autopaper": not self.auto_before}, headers=evil)
        self.assertEqual(res.status_code, 403, res.text)
        self.assertEqual(res.json()["error"]["code"], "CSRF_ORIGIN")
        self.assertEqual(bool(self.engine.params.auto_paper_orders), self.auto_before)
        res = admin.post("/api/v1/auth/login", json={"name": "x", "password": "y"}, headers={"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(res.status_code, 403)
        same = admin.get("/api/v1/strategy/pump-paper-v1", headers=evil)  # reads are not CSRF targets
        self.assertEqual(same.status_code, 200)
        ok_origin = admin.post("/api/v1/auth/logout", headers={"Origin": "http://testserver"})
        self.assertEqual(ok_origin.status_code, 200)
        from starlette.websockets import WebSocketDisconnect

        user = self._signed_in("csrfws")
        with self.assertRaises(WebSocketDisconnect):
            with user.websocket_connect("/api/v1/ws", headers=evil) as ws:
                ws.receive_json()

    def test_account_lockout_after_failures_across_ips(self):
        os.environ["AUU_AUTH_ACCOUNT_FAIL_MAX"] = "3"
        try:
            self._signed_in("lockme")
            client = self.TestClient(self.app)
            for _ in range(3):
                self.assertEqual(client.post("/api/v1/auth/login", json={"name": "lockme", "password": "wrong999x"}).status_code, 401)
            locked = client.post("/api/v1/auth/login", json={"name": "LockMe", "password": PW})
            self.assertEqual(locked.status_code, 429)
            self.assertEqual(locked.json()["error"]["code"], "ACCOUNT_LOCKED")
        finally:
            os.environ.pop("AUU_AUTH_ACCOUNT_FAIL_MAX", None)

    def test_cookie_flags(self):
        client = self.TestClient(self.app)
        body = {"name": "cookieuser", "password": PW, "password_confirm": PW}
        cookie = client.post("/api/v1/auth/register", json=body).headers.get("set-cookie", "").lower()
        self.assertIn("httponly", cookie)
        self.assertIn("samesite=lax", cookie)
        self.assertNotIn("secure", cookie)
        os.environ["AUU_COOKIE_SECURE"] = "on"
        try:
            again = client.post("/api/v1/auth/login", json={"name": "cookieuser", "password": PW})
            self.assertIn("secure", again.headers.get("set-cookie", "").lower())
        finally:
            os.environ.pop("AUU_COOKIE_SECURE", None)

    def test_live_cannot_be_enabled_over_http(self):
        from app.live.gate import evaluate, reset_live_state

        admin = self._signed_in("liveadmin")
        for method, path, body in [
            ("PUT", "/api/v1/live/enabled", {"liveEnabled": True, "confirmed": True}),
            ("PUT", "/api/v1/live/arm", {"armed": True}),
            ("PUT", "/api/v1/live/disabled", {"live_disabled": False}),
        ]:
            with self.subTest(path=path):
                res = admin.request(method, path, json=body)
                self.assertEqual(res.status_code, 403, res.text)
                self.assertIn("LIVE_API_LOCKED", res.json()["error"]["reasons"])
        self.assertFalse(evaluate().live_enabled)
        off = admin.put("/api/v1/live/enabled", json={"liveEnabled": False})
        self.assertEqual(off.status_code, 200, off.text)
        reset_live_state()


class AnonymousWsChannelTests(AuthGateApiTests):
    """Public-market WS stays open; private fills/signals/risk must not leak to anonymous clients.

    Hello omitting event names is not sufficient. Covers legacy and mainstream subscribe
    plus a provider that yields a private frame on a public channel.
    """

    def _synthetic_provider(self):
        class SyntheticProvider:
            name = "synthetic_probe"

            async def stream(self, channel, symbol, interval):
                if channel == "candles":
                    yield {"type": "candle", "payload": {"public_marker": "ok", "seq": 1, "symbol": symbol}}
                    # If subscribe checks were bypassed or a public stream mixed types, this must still be dropped.
                    yield {"type": "risk", "payload": {"private_marker": "leaked_on_public_channel"}}
                    yield {"type": "candle", "payload": {"public_marker": "ok", "seq": 2, "symbol": symbol}}
                elif channel in {"book", "trades"}:
                    typ = "book" if channel == "book" else "trade"
                    yield {"type": typ, "payload": {"public_marker": "ok", "symbol": symbol}}
                elif channel in {"risk", "fills", "signals"}:
                    typ = {"risk": "risk", "fills": "fill", "signals": "signal"}[channel]
                    yield {"type": typ, "payload": {"private_marker": "synthetic_probe", "channel": channel}}
                else:
                    yield {"type": "error", "payload": {"code": "UNEXPECTED", "message": str(channel)}}
                await asyncio.Event().wait()

        return SyntheticProvider()

    def test_anonymous_may_emit_rejects_private_and_unknown_frames(self):
        from app.routes.ws import anonymous_may_emit, PUBLIC_MARKET_CHANNELS, PRIVATE_ACCOUNT_CHANNELS

        self.assertEqual(PUBLIC_MARKET_CHANNELS, frozenset({"candles", "book", "trades"}))
        self.assertEqual(PRIVATE_ACCOUNT_CHANNELS, frozenset({"signals", "fills", "risk"}))
        for typ in ("candle", "book", "trade", "ticker", "kline", "rt_status", "hello", "subscribed", "error", "ping"):
            self.assertTrue(anonymous_may_emit({"type": typ}), typ)
        for typ in ("risk", "fill", "signal", "reject", "trading_state"):
            self.assertFalse(anonymous_may_emit({"type": typ, "payload": {"private_marker": "x"}}), typ)
            self.assertFalse(anonymous_may_emit('{"type":"%s","payload":{"private_marker":"x"}}' % typ), typ)
        self.assertFalse(anonymous_may_emit({"payload": {"private_marker": "x"}}))
        self.assertFalse(anonymous_may_emit("not-json"))

    def _assert_private_channels_rejected(self, ws):
        seen = []
        for channel in ("risk", "fills", "signals"):
            ws.send_json({"type": "subscribe", "channel": channel, "symbol": "SOL"})
            msg = ws.receive_json()
            seen.append((channel, msg))
            self.assertEqual(msg.get("type"), "error", msg)
            self.assertEqual(msg.get("payload", {}).get("code"), "AUTH_REQUIRED", msg)
            self.assertNotEqual(msg.get("type"), "subscribed")
        dumped = repr(seen)
        self.assertNotIn("private_marker", dumped)
        self.assertNotIn("synthetic_probe", dumped)

    def test_legacy_anonymous_cannot_subscribe_or_receive_private_frames(self):
        """Reproduces the independent-review probe: legacy on, auth on, anonymous risk subscribe."""
        self.app.state.legacy_pump = True
        try:
            with patch("app.routes.ws.get_provider", return_value=self._synthetic_provider()):
                anon = self.TestClient(self.app)
                with anon.websocket_connect("/api/v1/ws") as ws:
                    hello = ws.receive_json()
                    self.assertEqual(hello["type"], "hello")
                    self._assert_private_channels_rejected(ws)
                    ws.send_json({"type": "subscribe", "channel": "candles", "symbol": "SOL", "interval": "1h"})
                    frames = [ws.receive_json() for _ in range(3)]
                    types = [f.get("type") for f in frames]
                    self.assertEqual(types, ["subscribed", "candle", "candle"], frames)
                    self.assertEqual(frames[1]["payload"].get("seq"), 1)
                    self.assertEqual(frames[2]["payload"].get("seq"), 2)
                    for frame in frames:
                        self.assertNotIn("private_marker", repr(frame))
                    ws.send_json({"type": "subscribe", "channel": "book", "symbol": "SOL"})
                    book = [ws.receive_json() for _ in range(2)]
                    self.assertEqual([f.get("type") for f in book], ["subscribed", "book"], book)
                    self.assertIn("public_marker", book[1].get("payload", {}))
                    ws.send_json({"type": "subscribe", "channel": "trades", "symbol": "SOL"})
                    trades = [ws.receive_json() for _ in range(2)]
                    self.assertEqual([f.get("type") for f in trades], ["subscribed", "trade"], trades)
                    self.assertIn("public_marker", trades[1].get("payload", {}))
        finally:
            self.app.state.legacy_pump = False

    def test_legacy_signed_in_still_receives_private_provider_channels(self):
        """Logged-in permissions are not reduced by the anonymous whitelist."""
        self.app.state.legacy_pump = True
        try:
            with patch("app.routes.ws.get_provider", return_value=self._synthetic_provider()):
                user = self._signed_in("gatewspriv")
                with user.websocket_connect("/api/v1/ws") as ws:
                    hello = ws.receive_json()
                    self.assertEqual(hello["type"], "hello")
                    for name in ("risk", "fill", "signal"):
                        self.assertIn(name, hello.get("eventTypes", []), hello)
                    for channel, typ in (("risk", "risk"), ("fills", "fill"), ("signals", "signal")):
                        ws.send_json({"type": "subscribe", "channel": channel, "symbol": "SOL"})
                        ack = ws.receive_json()
                        frame = ws.receive_json()
                        self.assertEqual(ack.get("type"), "subscribed", ack)
                        self.assertEqual(ack.get("channel"), channel)
                        self.assertEqual(frame.get("type"), typ, frame)
                        self.assertEqual(frame.get("payload", {}).get("private_marker"), "synthetic_probe")
        finally:
            self.app.state.legacy_pump = False

    def test_mainstream_anonymous_private_subscribe_rejected_public_ws_stays_open(self):
        """Non-legacy: still a public WS; private channel subscribe is AUTH_REQUIRED, not a data leak."""
        self.app.state.legacy_pump = False
        anon = self.TestClient(self.app)
        with anon.websocket_connect("/api/v1/ws") as ws:
            hello = ws.receive_json()
            self.assertEqual(hello["type"], "hello")
            self.assertNotIn("fill", hello.get("eventTypes", []))
            self.assertNotIn("risk", hello.get("eventTypes", []))
            for channel in ("risk", "fills", "signals"):
                ws.send_json({"type": "subscribe", "channel": channel, "symbol": "SOL"})
                msg = ws.receive_json()
                self.assertEqual(msg.get("type"), "error", msg)
                self.assertEqual(msg.get("payload", {}).get("code"), "AUTH_REQUIRED", msg)
                self.assertNotIn("private_marker", repr(msg))
            # Public market provider channels stay unavailable when legacy is off (existing behavior).
            ws.send_json({"type": "subscribe", "channel": "candles", "symbol": "SOL", "interval": "1h"})
            candles = ws.receive_json()
            self.assertEqual(candles.get("type"), "error", candles)
            self.assertEqual(candles.get("payload", {}).get("code"), "LEGACY_OFF", candles)
            ws.send_json({"type": "ping"})
            self.assertEqual(ws.receive_json().get("type"), "pong")
