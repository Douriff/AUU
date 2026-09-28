"""GET /api/v1/board — read-only paper overview. Does not arm live or edit params."""
from __future__ import annotations

import os
import unittest

from app.models.contracts import Fill
from app.paper.broker import reset_paper_broker
from app.paper.ledger import get_paper_journal, reset_paper_ledger
from app.paper.shadow_compare import reset_shadow_compare
from app.providers import reset_provider
from app.risk.gate import reset_risk_gate
from app.strategies.pump_paper_v1 import reset_engine


class BoardApiTests(unittest.TestCase):
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
        reset_shadow_compare()
        from fastapi.testclient import TestClient
        from app.main import app

        cls.client = TestClient(app)

    @classmethod
    def tearDownClass(cls):
        os.environ["DATA_PROVIDER"] = "mock"
        os.environ["PUMP_PAPER_LOOP"] = "1"
        reset_provider()
        reset_engine()
        reset_risk_gate()
        reset_paper_broker()
        reset_paper_ledger()

    def setUp(self):
        reset_paper_ledger()
        reset_risk_gate()
        reset_paper_broker()
        reset_engine()
        reset_shadow_compare()

    def test_empty_board_is_paper_and_read_only(self):
        before = self.client.get("/api/v1/strategy/pump-paper-v1")
        self.assertEqual(before.status_code, 200)
        params = before.json()["data"]["params"]
        self.assertFalse(before.json()["data"]["auto_paper_orders"])

        r = self.client.get("/api/v1/board")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers.get("X-Api-Version"), "1")
        body = r.json()
        self.assertTrue(body["ok"])
        data = body["data"]
        self.assertEqual(data["mode"], "paper")
        self.assertFalse(data["liveEnabled"])
        self.assertTrue(data["liveDisabled"])
        self.assertTrue(data["empty"])
        self.assertEqual(data["stats"]["n_closed"], 0)
        self.assertIsNone(data["stats"]["win_rate"])
        self.assertIsNone(data["stats"]["expectancy"])
        self.assertEqual(data["stats"]["verdict"], "no-go")
        self.assertIn(data["stats"]["lamp"], {"gray", "red", "green"})
        self.assertEqual(data["stats"]["go_window_label"], "round8b")
        self.assertEqual(data["tape"], [])
        self.assertEqual(data["positions"], [])
        self.assertEqual(data["equity"], [])
        self.assertEqual(data["session_pnl"], 0)
        self.assertFalse(data["shadow"]["enabled"])
        self.assertFalse(data["shadow"]["liveEnabled"])
        self.assertIn("note", data["shadow"])
        self.assertGreaterEqual(len(data["ticker"]), 1)
        for item in data["ticker"]:
            self.assertTrue(item["symbol"])
            self.assertTrue(item["base"])
            self.assertIsInstance(item["change_pct"], (int, float))
            self.assertIsInstance(item["price"], (int, float))

        after = self.client.get("/api/v1/strategy/pump-paper-v1")
        self.assertEqual(after.json()["data"]["params"], params)
        self.assertFalse(after.json()["data"]["auto_paper_orders"])
        live = self.client.get("/api/v1/live/status")
        self.assertTrue(live.json()["ok"])
        self.assertFalse(live.json()["data"].get("liveEnabled", False))

    def test_closed_trades_equity_tape_and_open_lot(self):
        journal = get_paper_journal()
        journal.record_fill(
            "PUMPDEMO/SOL",
            Fill(ts=1_000, price=1.0, qty=10.0, fee=0.0, tag="paper:pump-paper-v1"),
            mint="DemoMintPump11111111111111111111111111111",
        )
        closed = journal.record_fill(
            "PUMPDEMO/SOL",
            Fill(ts=2_000, price=1.1, qty=-10.0, fee=0.0, tag="paper:pump-paper-v1:flat"),
            reason="take_profit",
            mint="DemoMintPump11111111111111111111111111111",
        )
        self.assertEqual(len(closed), 1)
        journal.record_fill(
            "PUMPDEMO/SOL",
            Fill(ts=3_000, price=2.0, qty=4.0, fee=0.0, tag="paper:pump-paper-v1"),
            mint="DemoMintPump11111111111111111111111111111",
        )
        journal.record_fill(
            "PUMPDEMO/SOL",
            Fill(ts=4_000, price=1.5, qty=-4.0, fee=0.0, tag="paper:pump-paper-v1:flat"),
            reason="stop_loss",
            mint="DemoMintPump11111111111111111111111111111",
        )
        journal.record_fill(
            "MOONMOCK/SOL",
            Fill(ts=5_000, price=0.5, qty=3.0, fee=0.0, tag="paper:pump-paper-v1"),
            mint="DemoMintMoon11111111111111111111111111111",
        )

        r = self.client.get("/api/v1/board")
        data = r.json()["data"]
        self.assertFalse(data["empty"])
        self.assertFalse(data["liveEnabled"])
        self.assertEqual(data["stats"]["n_closed"], 2)
        self.assertAlmostEqual(data["stats"]["win_rate"], 0.5)
        self.assertIsNotNone(data["stats"]["expectancy"])
        self.assertEqual(data["stats"]["verdict"], "no-go")

        self.assertGreaterEqual(len(data["equity"]), 3)
        self.assertEqual(data["equity"][0]["pnl"], 0.0)
        self.assertAlmostEqual(data["session_pnl"], data["equity"][-1]["pnl"])
        self.assertAlmostEqual(data["session_pnl"], closed[0].pnl + (1.5 - 2.0) * 4.0)

        self.assertEqual(data["tape"][0]["exit_reason"], "stop_loss")
        self.assertEqual(data["tape"][0]["exit_label"], "SL")
        self.assertLess(data["tape"][0]["net_bps"], 0)
        self.assertLess(data["tape"][0]["pnl"], 0)
        self.assertEqual(data["tape"][1]["exit_reason"], "take_profit")
        self.assertEqual(data["tape"][1]["exit_label"], "TP")
        self.assertAlmostEqual(data["tape"][1]["net_bps"], 1_000.0)
        self.assertGreater(data["tape"][1]["pnl"], 0)

        self.assertEqual(len(data["positions"]), 1)
        pos = data["positions"][0]
        self.assertEqual(pos["symbol"], "MOONMOCK/SOL")
        self.assertAlmostEqual(pos["qty"], 3.0)
        self.assertEqual(pos["source"], "journal")
        self.assertIn("upnl", pos)

        again = self.client.get("/api/v1/board").json()["data"]
        self.assertEqual(again["stats"]["n_closed"], 2)
        self.assertEqual(len(again["positions"]), 1)


if __name__ == "__main__":
    unittest.main()
