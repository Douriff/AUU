"""Read-only new_token discovery (paper watchlist only, no orders)."""
from __future__ import annotations

import inspect
import os
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

os.environ["PUMPFUN_DISCOVERY"] = "off"
os.environ.setdefault("DATA_PROVIDER", "pumpfun_paper")

from app.discovery import (
    DiscoveryRuntime,
    PORTAL_BACKOFF_CAP_SEC,
    PORTAL_BACKOFF_INITIAL_SEC,
    PORTAL_WS_DEFAULT,
    REASON_PORTAL_AUTH_REJECTED,
    build_portal_ws_uri,
    discovery_health_fields,
    next_portal_backoff,
    normalize_new_token,
    portal_api_key,
    portal_key_configured,
    reset_discovery,
    resolve_discovery_mode,
    sanitize_portal_api_key,
    should_fallback_to_logs,
    ws_http_status,
)
from app.models.contracts import NewTokenEvent
from app.paper.broker import get_paper_broker, reset_paper_broker
from app.providers import reset_provider
from app.providers.pumpfun_paper import PumpfunPaperProvider
from app.strategies.pump_paper_v1 import PumpPaperParams, TapeWindow, evaluate, reset_engine


PORTAL_SAMPLE = {
    "mint": "DiscMint111111111111111111111111111111111",
    "symbol": "NEWCOIN",
    "traderPublicKey": "Creator111111111111111111111111111111111",
    "vSolInBondingCurve": 30.0,
    "vTokensInBondingCurve": 1_073_000_000_000_000,
    "slot": 42,
    "timestamp": 1_700_000_000,
}


class ModeTests(unittest.TestCase):
    def test_default_off_without_key(self):
        os.environ.pop("PUMPFUN_DISCOVERY", None)
        os.environ.pop("PUMPFUN_PORTAL_API_KEY", None)
        self.assertEqual(resolve_discovery_mode(), "off")
        self.assertFalse(portal_key_configured())

    def test_default_portal_when_key(self):
        os.environ.pop("PUMPFUN_DISCOVERY", None)
        os.environ["PUMPFUN_PORTAL_API_KEY"] = "secret-not-for-repo"
        try:
            self.assertEqual(resolve_discovery_mode(), "pumpportal")
            self.assertTrue(portal_key_configured())
        finally:
            os.environ.pop("PUMPFUN_PORTAL_API_KEY", None)
            os.environ["PUMPFUN_DISCOVERY"] = "off"

    def test_explicit_off(self):
        os.environ["PUMPFUN_DISCOVERY"] = "off"
        os.environ["PUMPFUN_PORTAL_API_KEY"] = "x"
        try:
            self.assertEqual(resolve_discovery_mode(), "off")
        finally:
            os.environ.pop("PUMPFUN_PORTAL_API_KEY", None)

    def test_module_has_no_order_path(self):
        import app.discovery as disc

        src = inspect.getsource(disc)
        self.assertNotIn("from app.paper", src)
        self.assertNotIn("from app.risk", src)
        self.assertNotIn("run_paper_order(", src)
        self.assertIn('"subscribeNewToken"', src)
        self.assertIn("wss://pumpportal.fun/api/data", src)
        self.assertNotIn('"subscribeTokenTrade"', src)
        self.assertNotIn('"subscribeAccountTrade"', src)


class NormalizeTests(unittest.TestCase):
    def test_pumpportal_shape(self):
        ev = normalize_new_token(PORTAL_SAMPLE, "pumpportal")
        self.assertIsNotNone(ev)
        assert ev is not None
        self.assertEqual(ev.mint, PORTAL_SAMPLE["mint"])
        self.assertEqual(ev.creator, PORTAL_SAMPLE["traderPublicKey"])
        self.assertEqual(ev.slot, 42)
        self.assertEqual(ev.source, "pumpportal")
        self.assertIn("virtual_sol_reserves", ev.initial_reserves)
        self.assertGreater(int(ev.initial_reserves["virtual_sol_reserves"]), 0)
        dumped = ev.model_dump()
        for k in ("mint", "creator", "slot", "initial_reserves", "ts", "source"):
            self.assertIn(k, dumped)

    def test_logs_shape(self):
        ev = normalize_new_token(
            {
                "mint": "LogMint222222222222222222222222222222222",
                "creator": "LogCreator",
                "slot": 9,
                "ts": 1_700_000_000_000,
            },
            "logs",
        )
        self.assertIsNotNone(ev)
        assert ev is not None
        self.assertEqual(ev.source, "logs")
        self.assertEqual(ev.slot, 9)

    def test_rejects_bad_source_and_mint(self):
        self.assertIsNone(normalize_new_token(PORTAL_SAMPLE, "sniper"))
        self.assertIsNone(normalize_new_token({"symbol": "X"}, "pumpportal"))


class RegisterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        os.environ["DATA_PROVIDER"] = "pumpfun_paper"
        os.environ["PUMPFUN_DISCOVERY"] = "off"
        reset_provider()
        reset_engine()
        reset_paper_broker()

    async def test_ingest_registers_watchlist_no_fill(self):
        from app.providers import get_provider

        runtime = DiscoveryRuntime()
        broker = get_paper_broker()
        before = list(broker.open_orders)
        ev = await runtime.ingest(PORTAL_SAMPLE, "pumpportal")
        self.assertIsInstance(ev, NewTokenEvent)
        p = get_provider()
        syms = {s.symbol: s for s in p.list_symbols()}
        self.assertIn("NEWCOIN/SOL", syms)
        flags = p.watch_flags("NEWCOIN/SOL")
        self.assertTrue(flags.get("discovered"))
        snap = p.get_pumpfun_snapshot("NEWCOIN/SOL")
        self.assertIsNotNone(snap)
        assert snap is not None
        self.assertLess(snap.progress_bps, 800)
        self.assertEqual(broker.open_orders, before)
        # discovery ≠ entry
        sig = evaluate(
            snapshot=snap,
            tape=TapeWindow(buy_notional_1m=10, sell_notional_1m=1, trade_count_1m=20),
            params=PumpPaperParams(),
            now_ms=snap.updated_ts,
            impact_entry_bps=10.0,
        )
        self.assertEqual(sig.side, "flat")
        self.assertEqual(sig.reason, "progress_band")
        from app.strategies.pump_paper_v1 import get_engine

        row = next(r for r in get_engine().monitor_rows() if r["symbol"] == "NEWCOIN/SOL")
        self.assertIn("discovered", row["tags"])
        self.assertIn("pumpportal", row["tags"])

    async def test_duplicate_mint_ignored(self):
        runtime = DiscoveryRuntime()
        first = await runtime.ingest(PORTAL_SAMPLE, "pumpportal")
        second = await runtime.ingest(PORTAL_SAMPLE, "pumpportal")
        self.assertIsNotNone(first)
        self.assertIsNone(second)

    async def test_symbol_collision_uniquifies(self):
        from app.providers import get_provider

        runtime = DiscoveryRuntime()
        await runtime.ingest(PORTAL_SAMPLE, "pumpportal")
        other = dict(PORTAL_SAMPLE)
        other["mint"] = "DiscMint222222222222222222222222222222222"
        second = await runtime.ingest(other, "pumpportal")
        self.assertIsNotNone(second)
        names = {s.symbol for s in get_provider().list_symbols()}
        self.assertIn("NEWCOIN/SOL", names)
        self.assertIn("NEWCOIN1/SOL", names)


class DecodeLogsTests(unittest.TestCase):
    def test_extract_create_from_logs(self):
        import base64
        import struct

        from app.providers.pumpfun_decode import extract_create_from_logs

        def bstr(s: str) -> bytes:
            raw = s.encode()
            return struct.pack("<I", len(raw)) + raw

        body = bstr("foo") + bstr("BAR") + bstr("x") + (b"\x01" * 32) + (b"\x02" * 32) + (b"\x03" * 32)
        b64 = base64.b64encode(b"\x00" * 8 + body).decode()
        parsed = extract_create_from_logs(
            ["Program log: Instruction: Create", f"Program data: {b64}"]
        )
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed["symbol"], "BAR")
        self.assertTrue(parsed["mint"])
        self.assertTrue(parsed["creator"])
        self.assertIsNone(
            extract_create_from_logs(["Program log: Instruction: Buy", f"Program data: {b64}"])
        )


class HealthDiscoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["DATA_PROVIDER"] = "pumpfun_paper"
        os.environ["PUMPFUN_DISCOVERY"] = "off"
        os.environ["PUMP_PAPER_LOOP"] = "0"
        reset_provider()
        from fastapi.testclient import TestClient
        from app.main import app

        cls.client = TestClient(app)

    def test_health_discovery_fields_no_key_leak(self):
        r = self.client.get("/api/v1/health")
        data = r.json()["data"]
        self.assertEqual(data["discovery"], "off")
        self.assertEqual(data["discoveryActive"], "off")
        self.assertEqual(data["discoveryReason"], "off")
        self.assertEqual(data["discoveryOptions"], ["pumpportal", "logs", "off"])
        self.assertIn("portal_key_configured", data)
        self.assertFalse(data.get("liveEnabled", False))
        blob = r.text.lower()
        self.assertNotIn("api-key=", blob)
        self.assertNotIn("secret-not-for-repo", blob)

    def test_ws_hello_lists_new_token(self):
        with self.client.websocket_connect("/api/v1/ws") as ws:
            hello = ws.receive_json()
            self.assertIn("new_token", hello.get("eventTypes", []))


class SanitizeKeyTests(unittest.TestCase):
    def setUp(self):
        reset_discovery()
        os.environ.pop("PUMPFUN_PORTAL_API_KEY", None)

    def tearDown(self):
        os.environ.pop("PUMPFUN_PORTAL_API_KEY", None)
        reset_discovery()

    def test_strips_quotes_and_whitespace(self):
        self.assertEqual(sanitize_portal_api_key('  "abc123"  '), "abc123")
        self.assertEqual(sanitize_portal_api_key("'xyz'"), "xyz")
        self.assertEqual(sanitize_portal_api_key("   "), "")
        self.assertEqual(sanitize_portal_api_key(None), "")

    def test_multi_token_uses_first_never_concatenates(self):
        first = "A" * 103
        second = "B" * 103
        raw = f'"{first} {second}"'
        key = sanitize_portal_api_key(raw)
        self.assertEqual(key, first)
        self.assertNotEqual(key, first + " " + second)
        self.assertNotIn(" ", key)
        uri = build_portal_ws_uri(key=key)
        self.assertTrue(uri.startswith(PORTAL_WS_DEFAULT + "?"))
        qs = parse_qs(urlsplit(uri).query)
        self.assertEqual(qs.get("api-key"), [first])
        self.assertNotIn(second, uri)
        self.assertNotIn("%20", uri)

    def test_env_quoted_two_keys_do_not_enter_ws_uri(self):
        first = "tok-one-" + ("x" * 94)
        second = "tok-two-" + ("y" * 94)
        os.environ["PUMPFUN_PORTAL_API_KEY"] = f'"{first} {second}"'
        self.assertEqual(len(first), 103)
        self.assertEqual(portal_api_key(), first)
        uri = build_portal_ws_uri(key=portal_api_key())
        self.assertEqual(parse_qs(urlsplit(uri).query)["api-key"], [first])
        self.assertNotIn(second, uri)

    def test_warns_without_logging_the_key(self):
        secret_a = "never-log-key-aaaa"
        secret_b = "never-log-key-bbbb"
        with self.assertLogs("auu.discovery", level="WARNING") as cm:
            sanitize_portal_api_key(f"{secret_a} {secret_b}")
        blob = "\n".join(cm.output)
        self.assertIn("whitespace-separated", blob)
        self.assertNotIn(secret_a, blob)
        self.assertNotIn(secret_b, blob)

    def test_portal_ws_default_matches_docs(self):
        self.assertEqual(PORTAL_WS_DEFAULT, "wss://pumpportal.fun/api/data")
        uri = build_portal_ws_uri(key="k")
        self.assertEqual(uri, "wss://pumpportal.fun/api/data?api-key=k")


class BackoffFallbackTests(unittest.TestCase):
    def test_exponential_cap_five_minutes(self):
        cur = PORTAL_BACKOFF_INITIAL_SEC
        delays = []
        for _ in range(8):
            delay, cur = next_portal_backoff(cur)
            delays.append(delay)
        self.assertEqual(delays[0], 5.0)
        self.assertEqual(delays[1], 10.0)
        self.assertEqual(delays[2], 20.0)
        self.assertEqual(delays[-1], PORTAL_BACKOFF_CAP_SEC)
        self.assertEqual(PORTAL_BACKOFF_CAP_SEC, 300.0)
        self.assertTrue(all(d <= 300.0 for d in delays))

    def test_fallback_only_when_rpc_set(self):
        self.assertFalse(should_fallback_to_logs(rpc_url="", fallback_flag=""))
        self.assertTrue(should_fallback_to_logs(rpc_url="https://rpc.example", fallback_flag=""))
        self.assertFalse(
            should_fallback_to_logs(rpc_url="https://rpc.example", fallback_flag="off")
        )

    def test_health_fields_surface_portal_auth_rejected(self):
        reset_discovery()
        os.environ["PUMPFUN_DISCOVERY"] = "pumpportal"
        os.environ["PUMPFUN_PORTAL_API_KEY"] = "unit-test-key"
        try:
            rt = DiscoveryRuntime()
            rt.status_reason = REASON_PORTAL_AUTH_REJECTED
            rt.mode = "logs"
            import app.discovery as disc

            disc._engine = rt
            fields = discovery_health_fields()
            self.assertEqual(fields["discovery"], "pumpportal")
            self.assertEqual(fields["discoveryActive"], "logs")
            self.assertEqual(fields["discoveryReason"], "portal_auth_rejected")
            self.assertTrue(fields["portal_key_configured"])
        finally:
            os.environ["PUMPFUN_DISCOVERY"] = "off"
            os.environ.pop("PUMPFUN_PORTAL_API_KEY", None)
            reset_discovery()


class MockPortalWsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        reset_discovery()
        os.environ["PUMPFUN_DISCOVERY"] = "pumpportal"
        os.environ["PUMPFUN_PORTAL_API_KEY"] = "unit-portal-key"
        os.environ["PUMPFUN_DISCOVERY_FALLBACK"] = "off"
        os.environ.pop("SOLANA_RPC_URL", None)

    def tearDown(self):
        os.environ["PUMPFUN_DISCOVERY"] = "off"
        os.environ.pop("PUMPFUN_PORTAL_API_KEY", None)
        os.environ.pop("PUMPFUN_DISCOVERY_FALLBACK", None)
        os.environ.pop("SOLANA_RPC_URL", None)
        reset_discovery()

    def _invalid_status(self, code: int):
        from websockets.datastructures import Headers
        from websockets.exceptions import InvalidStatus
        from websockets.http11 import Response

        return InvalidStatus(Response(code, "Rejected", Headers()))

    async def test_http_403_backoff_without_tight_retry_storm(self):
        runtime = DiscoveryRuntime()
        sleeps: list[float] = []
        seen_uris: list[str] = []

        def connect(uri, *a, **k):
            seen_uris.append(uri)
            raise self._invalid_status(403)

        async def fake_sleep(delay):
            sleeps.append(float(delay))
            if len(sleeps) >= 8:
                runtime.stop()

        with patch("websockets.connect", side_effect=connect), patch("asyncio.sleep", fake_sleep):
            await runtime.run_loop()

        self.assertEqual(runtime.status_reason, REASON_PORTAL_AUTH_REJECTED)
        self.assertEqual(runtime.mode, "pumpportal")
        self.assertGreaterEqual(len(sleeps), 8)
        self.assertEqual(sleeps[0], 5.0)
        self.assertEqual(sleeps[1], 10.0)
        self.assertEqual(sleeps[-1], 300.0)
        self.assertTrue(all("unit-portal-key" in u for u in seen_uris))
        self.assertTrue(all(u.startswith("wss://pumpportal.fun/api/data?api-key=") for u in seen_uris))

    async def test_http_400_multi_key_uri_uses_first_token_then_fallback_logs(self):
        first = "C" * 103
        second = "D" * 103
        os.environ["PUMPFUN_PORTAL_API_KEY"] = f'"{first} {second}"'
        os.environ["SOLANA_RPC_URL"] = "https://rpc.test.invalid"
        os.environ.pop("PUMPFUN_DISCOVERY_FALLBACK", None)
        runtime = DiscoveryRuntime()
        seen_uris: list[str] = []
        logs_entered = {"n": 0}

        class LogsCM:
            async def __aenter__(self):
                logs_entered["n"] += 1
                runtime.stop()
                return self

            async def __aexit__(self, *a):
                return False

            async def send(self, *_a, **_k):
                return None

            async def recv(self):
                raise TimeoutError

        def connect(uri, *a, **k):
            seen_uris.append(uri)
            if "pumpportal.fun" in uri:
                raise self._invalid_status(400)
            return LogsCM()

        with patch("websockets.connect", side_effect=connect):
            await runtime.run_loop()

        self.assertEqual(runtime.status_reason, REASON_PORTAL_AUTH_REJECTED)
        self.assertEqual(runtime.mode, "logs")
        self.assertEqual(logs_entered["n"], 1)
        portal_uris = [u for u in seen_uris if "pumpportal.fun" in u]
        self.assertTrue(portal_uris)
        qs = parse_qs(urlsplit(portal_uris[0]).query)
        self.assertEqual(qs.get("api-key"), [first])
        self.assertNotIn(second, portal_uris[0])
        self.assertTrue(any("rpc.test.invalid" in u for u in seen_uris))

    async def test_ws_http_status_from_invalid_status(self):
        self.assertEqual(ws_http_status(self._invalid_status(400)), 400)
        self.assertEqual(ws_http_status(self._invalid_status(403)), 403)


if __name__ == "__main__":
    unittest.main()
