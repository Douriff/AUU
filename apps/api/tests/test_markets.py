"""GET /api/v1/markets — read-only token list with sparkline history."""
from __future__ import annotations

import os
import unittest

from app.paper.broker import reset_paper_broker
from app.paper.ledger import reset_paper_ledger
from app.providers import reset_provider
from app.risk.gate import reset_risk_gate
from app.strategies.pump_paper_v1 import reset_engine


class MarketsApiTests(unittest.TestCase):
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

    def setUp(self):
        os.environ["DATA_PROVIDER"] = "pumpfun_paper"
        os.environ["PUMPFUN_DISCOVERY"] = "off"
        reset_provider()
        reset_engine()

    def test_pumpfun_list_has_spark_cap_and_stays_read_only(self):
        before = self.client.get("/api/v1/strategy/pump-paper-v1")
        self.assertEqual(before.status_code, 200)
        params = before.json()["data"]["params"]

        r = self.client.get("/api/v1/markets")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["ok"])
        data = body["data"]
        self.assertEqual(data["mode"], "paper")
        self.assertFalse(data["liveEnabled"])
        self.assertTrue(data["liveDisabled"])
        self.assertFalse(data["usd_available"])
        self.assertEqual(data["quote"], "SOL")
        self.assertEqual(data["discovery"], "off")
        self.assertFalse(data["empty"])
        self.assertGreaterEqual(len(data["items"]), 1)

        symbols = {item["symbol"] for item in data["items"]}
        self.assertIn("PUMPDEMO/SOL", symbols)
        for item in data["items"]:
            self.assertTrue(item["base"])
            self.assertIsInstance(item["price_sol"], (int, float))
            self.assertIsInstance(item["change_pct"], (int, float))
            self.assertIsInstance(item["volume_sol"], (int, float))
            self.assertIsInstance(item["market_cap_sol"], (int, float))
            self.assertGreaterEqual(item["market_cap_sol"], 0)
            self.assertIsInstance(item["progress_bps"], int)
            self.assertAlmostEqual(item["progress_pct"], item["progress_bps"] / 100.0)
            self.assertIsNone(item["price_usd"])
            self.assertGreaterEqual(len(item["spark"]), 2)
            self.assertTrue(all(isinstance(p, (int, float)) and p > 0 for p in item["spark"]))
            self.assertLessEqual(len(item["spark"]), 28)
        self.assertTrue(any(item["market_cap_sol"] > 0 for item in data["items"]))

        after = self.client.get("/api/v1/strategy/pump-paper-v1")
        self.assertEqual(after.json()["data"]["params"], params)
        self.assertFalse(after.json()["data"]["auto_paper_orders"])
        live = self.client.get("/api/v1/live/status")
        self.assertFalse(live.json()["data"].get("liveEnabled", False))

        again = self.client.get("/api/v1/markets").json()["data"]
        self.assertEqual(
            [i["symbol"] for i in again["items"]],
            [i["symbol"] for i in data["items"]],
        )

    def test_mock_provider_degrades_without_curve_fields(self):
        os.environ["DATA_PROVIDER"] = "mock"
        reset_provider()
        reset_engine()
        try:
            data = self.client.get("/api/v1/markets").json()["data"]
        finally:
            os.environ["DATA_PROVIDER"] = "pumpfun_paper"
            reset_provider()
            reset_engine()
        self.assertEqual(data["provider"], "mock")
        self.assertFalse(data["liveEnabled"])
        self.assertFalse(data["empty"])
        self.assertGreaterEqual(len(data["items"]), 1)
        sample = data["items"][0]
        self.assertIsNone(sample["market_cap_sol"])
        self.assertIsNone(sample["progress_bps"])
        self.assertIsNone(sample["price_usd"])
        self.assertGreaterEqual(len(sample["spark"]), 2)
        self.assertEqual(sample["volume_sol"], 0)


if __name__ == "__main__":
    unittest.main()
