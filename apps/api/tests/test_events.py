"""GET /api/v1/events — real paper console feed. Does not arm live or edit params."""
from __future__ import annotations

import asyncio
import os
import time
import unittest

from app.models.contracts import DecisionLogRow, Fill
from app.paper.broker import reset_paper_broker
from app.paper.decision_log import append_decision, reset_decision_log
from app.paper.events import reset_events, shanghai_day_start_ms
from app.paper.ledger import get_paper_journal, reset_paper_ledger
from app.legacy.pump.paper.shadow_compare import reset_shadow_compare
from app.providers import reset_provider
from app.risk.gate import reset_risk_gate
from app.legacy.pump.strategies.pump_paper_v1 import reset_engine


class EventsApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["DATA_PROVIDER"] = "pumpfun_paper"
        os.environ["PUMP_PAPER_LOOP"] = "0"
        os.environ["PUMPFUN_DISCOVERY"] = "off"
        reset_provider()
        reset_engine()
        reset_risk_gate()
        reset_paper_broker()
        reset_paper_ledger()
        reset_decision_log()
        reset_shadow_compare()
        reset_events()
        from fastapi.testclient import TestClient
        from app.main import app

        cls.client = TestClient(app)

    @classmethod
    def tearDownClass(cls):
        os.environ["DATA_PROVIDER"] = "mock"
        os.environ["PUMP_PAPER_LOOP"] = "1"
        os.environ.pop("PUMPFUN_DISCOVERY", None)
        reset_provider()
        reset_engine()
        reset_risk_gate()
        reset_paper_broker()
        reset_paper_ledger()
        reset_decision_log()
        reset_shadow_compare()
        reset_events()

    def setUp(self):
        reset_events()
        reset_paper_ledger()
        reset_decision_log()
        reset_shadow_compare()
        reset_risk_gate()
        reset_paper_broker()
        reset_engine()

    def test_feed_is_incremental_and_read_only(self):
        before = self.client.get("/api/v1/strategy/pump-paper-v1")
        params = before.json()["data"]["params"]
        self.assertFalse(before.json()["data"]["auto_paper_orders"])

        journal = get_paper_journal()
        journal.record_fill(
            "PUMPDEMO/SOL",
            Fill(ts=1_700_000_000_000, price=1.0, qty=10.0, fee=0.0, tag="paper:pump-paper-v1"),
            mint="DemoMintPump11111111111111111111111111111",
        )
        first = self.client.get("/api/v1/events")
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.headers.get("X-Api-Version"), "1")
        body = first.json()
        self.assertTrue(body["ok"])
        data = body["data"]
        self.assertEqual(data["mode"], "paper")
        self.assertFalse(data["liveEnabled"])
        self.assertTrue(data["liveDisabled"])
        self.assertIn("entry", data["types"])
        entries = [row for row in data["events"] if row["type"] == "entry"]
        self.assertEqual(len(entries), 1)
        self.assertIn("纸面开仓", entries[0]["message"])
        self.assertEqual(entries[0]["pill"], "开仓")
        cursor = data["cursor"]
        self.assertTrue(cursor)

        systems = [row for row in data["events"] if row["type"] == "system"]
        self.assertTrue(any("发现离线" in row["message"] or "发现" in row["message"] for row in systems))
        self.assertTrue(any("自动纸面" in row["message"] for row in systems))

        journal.record_fill(
            "PUMPDEMO/SOL",
            Fill(ts=1_700_000_005_000, price=1.1, qty=-10.0, fee=0.0, tag="paper:pump-paper-v1:flat"),
            reason="take_profit",
            mint="DemoMintPump11111111111111111111111111111",
        )
        second = self.client.get("/api/v1/events", params={"since": cursor})
        newer = second.json()["data"]["events"]
        self.assertTrue(newer)
        self.assertTrue(all(int(row["cursor"]) > int(cursor) for row in newer))
        exits = [row for row in newer if row["type"] == "exit"]
        self.assertEqual(len(exits), 1)
        self.assertEqual(exits[0]["pill"], "TP")
        self.assertGreater(exits[0]["pnl"], 0)
        self.assertAlmostEqual(exits[0]["net_bps"], 1000.0)
        self.assertNotIn("entry", {row["type"] for row in newer})

        after = self.client.get("/api/v1/strategy/pump-paper-v1")
        self.assertEqual(after.json()["data"]["params"], params)
        self.assertFalse(after.json()["data"]["auto_paper_orders"])
        live = self.client.get("/api/v1/live/status")
        self.assertFalse(live.json()["data"].get("liveEnabled", False))

    def test_shanghai_day_stats_and_risk_reject(self):
        start = shanghai_day_start_ms()
        journal = get_paper_journal()
        journal.record_fill(
            "PUMPDEMO/SOL",
            Fill(ts=start - 5_000, price=1.0, qty=2.0, fee=0.0, tag="paper:pump-paper-v1"),
            mint="DemoMintPump11111111111111111111111111111",
        )
        journal.record_fill(
            "PUMPDEMO/SOL",
            Fill(ts=start - 1_000, price=0.9, qty=-2.0, fee=0.0, tag="paper:pump-paper-v1:flat"),
            reason="stop_loss",
            mint="DemoMintPump11111111111111111111111111111",
        )
        journal.record_fill(
            "MOONMOCK/SOL",
            Fill(ts=start + 1_000, price=1.0, qty=4.0, fee=0.0, tag="paper:pump-paper-v1"),
            mint="DemoMintMoon11111111111111111111111111111",
        )
        journal.record_fill(
            "MOONMOCK/SOL",
            Fill(ts=start + 2_000, price=1.25, qty=-4.0, fee=0.0, tag="paper:pump-paper-v1:flat"),
            reason="sell_pressure",
            mint="DemoMintMoon11111111111111111111111111111",
        )
        journal.record_fill(
            "GRADMOCK/SOL",
            Fill(ts=start + 3_000, price=2.0, qty=1.0, fee=0.0, tag="paper:pump-paper-v1"),
            mint="DemoMintGrad11111111111111111111111111111",
        )
        append_decision(
            DecisionLogRow(
                ts=start + 4_000,
                strategy_id="pump-paper-v1",
                symbol="PUMPDEMO/SOL",
                stage="pre_order",
                outcome="reject",
                signal_reason="impact",
                risk_tags=["SLIPPAGE_CAP"],
                reject_bucket="impact",
                impact_gross_bps=8420,
                impact_bps_cap=80,
            )
        )
        append_decision(
            DecisionLogRow(
                ts=start + 5_000,
                strategy_id="pump-paper-v1",
                symbol="PUMPDEMO/SOL",
                stage="signal",
                outcome="reject",
                signal_reason="progress_band",
                reject_bucket="progress",
            )
        )

        data = self.client.get("/api/v1/events").json()["data"]
        stats = data["stats"]
        self.assertEqual(stats["closed_today"], 1)
        self.assertEqual(stats["open_positions"], 1)
        self.assertAlmostEqual(stats["pnl_today"], 1.0)
        self.assertAlmostEqual(stats["avg_net_bps"], 2500.0)
        self.assertEqual(stats["verdict"], "no-go")
        self.assertEqual(stats["go_window_label"], "round8b")
        self.assertFalse(stats["liveEnabled"])
        self.assertEqual(stats["mode"], "paper")

        exits = [row for row in data["events"] if row["type"] == "exit"]
        pills = {row["pill"] for row in exits}
        self.assertIn("SL", pills)
        self.assertIn("weak-tape", pills)
        rejects = [row for row in data["events"] if row["type"] == "reject"]
        self.assertEqual(len(rejects), 1)
        self.assertIn("8420", rejects[0]["message"])
        self.assertIn("80", rejects[0]["message"])
        self.assertNotIn("progress", {row["type"] for row in data["events"]})

    def test_discovery_event_from_ingest(self):
        from app.legacy.pump.discovery import DiscoveryRuntime

        runtime = DiscoveryRuntime()
        mint = "DiscMint111111111111111111111111111111111"
        ev = asyncio.run(
            runtime.ingest(
                {"mint": mint, "symbol": "DISC", "ts": int(time.time() * 1000)},
                "logs",
            )
        )
        self.assertIsNotNone(ev)
        data = self.client.get("/api/v1/events").json()["data"]
        found = [row for row in data["events"] if row["type"] == "discovery"]
        self.assertEqual(len(found), 1)
        self.assertIn("DISC", found[0]["message"])
        self.assertEqual(found[0]["mint"], mint)
        self.assertFalse(data["liveEnabled"])
