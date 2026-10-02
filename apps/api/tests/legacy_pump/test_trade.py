"""Manual paper ticket: limits, fill path, and exclusion from autopaper stats."""
from __future__ import annotations

import os
import unittest
from pathlib import Path

from app.models.contracts import Fill
from app.paper.broker import reset_paper_broker
from app.paper.decision_log import reset_decision_log
from app.paper.events import reset_events
from app.legacy.pump.paper.executability import build_executability
from app.paper.ledger import _ids_from_tag, get_paper_journal, reset_paper_ledger
from app.legacy.pump.paper.shadow_compare import build_shadow_compare, reset_shadow_compare
from app.providers import reset_provider
from app.risk.gate import get_risk_gate, reset_risk_gate
from app.legacy.pump.strategies.pump_paper_v1 import reset_engine


class TradeTicketTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["DATA_PROVIDER"] = "pumpfun_paper"
        os.environ["PUMP_PAPER_LOOP"] = "0"
        os.environ["PUMPFUN_DISCOVERY"] = "off"
        os.environ.pop("PUMPFUN_WATCH_MINTS", None)
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
        reset_decision_log()
        reset_shadow_compare()
        reset_events()

    def _strategy(self) -> dict:
        body = self.client.get("/api/v1/strategy/pump-paper-v1")
        self.assertEqual(body.status_code, 200)
        return body.json()["data"]

    def test_module_has_no_live_path(self):
        src = Path("app/legacy/pump/paper/manual_trade.py").read_text(encoding="utf-8")
        self.assertNotIn("app.live", src)
        self.assertNotIn("sendTransaction", src.lower())
        self.assertNotIn("update_params", src)

    def test_preview_quotes_impact_fee_and_stays_paper(self):
        r = self.client.get(
            "/api/v1/trade/preview",
            params={"symbol": "PUMPDEMO/SOL", "side": "buy", "notional_sol": 0.1},
        )
        self.assertEqual(r.status_code, 200)
        data = r.json()["data"]
        self.assertFalse(data["liveEnabled"])
        self.assertTrue(data["liveDisabled"])
        self.assertEqual(data["mode"], "paper")
        self.assertEqual(data["source"], "manual")
        self.assertGreater(data["impact_bps"], 0)
        self.assertGreater(data["fee_bps"], 0)
        self.assertGreater(data["fee_sol"], 0)
        self.assertGreater(data["expected_price"], 0)
        self.assertGreater(data["expected_qty"], 0)
        self.assertFalse(data["blocked"])
        self.assertEqual(data["limits"]["max_notional_sol"], 1.0)
        self.assertEqual(data["limits"]["max_open_positions"], 10)
        self.assertAlmostEqual(data["limits"]["max_day_loss_pct"], 0.045)

    def test_buy_above_one_sol_is_rejected(self):
        before = self._strategy()
        r = self.client.post(
            "/api/v1/trade/orders",
            json={"symbol": "PUMPDEMO/SOL", "side": "buy", "notional_sol": 1.01},
        )
        self.assertEqual(r.status_code, 400)
        err = r.json()["error"]
        self.assertEqual(err["code"], "MAX_NOTIONAL")
        self.assertFalse(get_paper_journal().lots)
        after = self._strategy()
        self.assertEqual(after["params"], before["params"])
        self.assertFalse(after["auto_paper_orders"])
        live = self.client.get("/api/v1/live/status").json()["data"]
        self.assertFalse(live.get("liveEnabled", False))

    def test_one_sol_on_graduated_curve_fills_as_manual(self):
        r = self.client.post(
            "/api/v1/trade/orders",
            json={"symbol": "GRADMOCK/SOL", "side": "buy", "notional_sol": 1.0},
        )
        self.assertEqual(r.status_code, 200)
        data = r.json()["data"]
        self.assertFalse(data["liveEnabled"])
        self.assertTrue(data["submitted"])
        self.assertIsNone(data.get("reject"))
        lots = get_paper_journal().lots["GRADMOCK/SOL"]
        self.assertEqual(len(lots), 1)
        sid, source = _ids_from_tag(lots[0].tag)
        self.assertEqual(sid, "manual-paper")
        self.assertEqual(source, "manual")
        self.assertIn("source=manual", lots[0].tag)

    def test_eleventh_name_is_blocked(self):
        journal = get_paper_journal()
        for i in range(10):
            journal.record_fill(
                f"M{i}/SOL",
                Fill(ts=1_000 + i, price=1.0, qty=1.0, fee=0.0, tag="paper:pump-paper-v1"),
            )
        r = self.client.post(
            "/api/v1/trade/orders",
            json={"symbol": "PUMPDEMO/SOL", "side": "buy", "notional_sol": 0.1},
        )
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["error"]["code"], "MAX_OPEN_MINTS")
        self.assertNotIn("PUMPDEMO/SOL", get_paper_journal().lots)

    def test_day_loss_blocks_buy_and_still_allows_close(self):
        opened = self.client.post(
            "/api/v1/trade/orders",
            json={"symbol": "PUMPDEMO/SOL", "side": "buy", "notional_sol": 0.1},
        )
        self.assertEqual(opened.status_code, 200)
        self.assertTrue(opened.json()["data"]["submitted"])
        get_risk_gate()._day_pnl = -450.0
        blocked = self.client.post(
            "/api/v1/trade/orders",
            json={"symbol": "PUMPDEMO/SOL", "side": "buy", "notional_sol": 0.1},
        )
        self.assertEqual(blocked.status_code, 400)
        self.assertEqual(blocked.json()["error"]["code"], "DAY_LOSS_BREAKER")
        closed = self.client.post(
            "/api/v1/trade/orders",
            json={"symbol": "PUMPDEMO/SOL", "side": "sell", "sell_pct": 100},
        )
        self.assertEqual(closed.status_code, 200)
        self.assertTrue(closed.json()["data"]["submitted"])
        self.assertNotIn("PUMPDEMO/SOL", get_paper_journal().lots)
        self.assertEqual(len(get_paper_journal().closed), 1)
        self.assertEqual(get_paper_journal().closed[0].source, "manual")

    def test_manual_round_trip_skips_gonogo_board_and_shadow(self):
        before_exec = build_executability()["n_closed"]
        before_board = self.client.get("/api/v1/board").json()["data"]["stats"]["n_closed"]
        before_shadow = build_shadow_compare()["main"]["n"]
        buy = self.client.post(
            "/api/v1/trade/orders",
            json={"mint": "DemoMintPump11111111111111111111111111111", "side": "buy", "notional_sol": 0.1},
        )
        self.assertEqual(buy.status_code, 200, buy.text)
        self.assertTrue(buy.json()["data"]["submitted"])
        sell = self.client.post(
            "/api/v1/trade/orders",
            json={"symbol": "PUMPDEMO/SOL", "side": "sell", "sell_pct": 100},
        )
        self.assertEqual(sell.status_code, 200, sell.text)
        self.assertEqual(len(get_paper_journal().closed), 1)
        self.assertEqual(build_executability()["n_closed"], before_exec)
        board = self.client.get("/api/v1/board").json()["data"]
        self.assertEqual(board["stats"]["n_closed"], before_board)
        self.assertFalse(board["liveEnabled"])
        self.assertEqual(build_shadow_compare()["main"]["n"], before_shadow)
        events = self.client.get("/api/v1/events").json()["data"]["events"]
        messages = " ".join(row["message"] for row in events)
        self.assertIn("手动开仓", messages)
        self.assertIn("手动平仓", messages)
        live = self.client.get("/api/v1/live/status").json()["data"]
        self.assertFalse(live.get("liveEnabled", False))


if __name__ == "__main__":
    unittest.main()
