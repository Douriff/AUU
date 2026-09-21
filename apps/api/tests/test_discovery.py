"""Read-only new_token discovery (paper watchlist only, no orders)."""
from __future__ import annotations

import inspect
import os
import unittest

os.environ["PUMPFUN_DISCOVERY"] = "off"
os.environ.setdefault("DATA_PROVIDER", "pumpfun_paper")

from app.discovery import (
    DiscoveryRuntime,
    normalize_new_token,
    portal_key_configured,
    resolve_discovery_mode,
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
        self.assertEqual(data["discoveryOptions"], ["pumpportal", "logs", "off"])
        self.assertIn("portal_key_configured", data)
        blob = r.text.lower()
        self.assertNotIn("api-key=", blob)
        self.assertNotIn("secret-not-for-repo", blob)

    def test_ws_hello_lists_new_token(self):
        with self.client.websocket_connect("/api/v1/ws") as ws:
            hello = ws.receive_json()
            self.assertIn("new_token", hello.get("eventTypes", []))


if __name__ == "__main__":
    unittest.main()
