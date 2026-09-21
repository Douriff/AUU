"""Pipeline decide-and-fill + GET /book /curve (paper-only)."""
from __future__ import annotations

import os
import unittest

os.environ.setdefault("DATA_PROVIDER", "pumpfun_paper")

from fastapi.testclient import TestClient

from app.providers import reset_provider
from app.risk import get_risk_gate


class PipelineApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["DATA_PROVIDER"] = "pumpfun_paper"
        reset_provider()
        from app.main import app

        cls.client = TestClient(app)

    @classmethod
    def tearDownClass(cls):
        os.environ["DATA_PROVIDER"] = "mock"
        reset_provider()

    def setUp(self):
        gate = get_risk_gate()
        gate._cooldown_until.clear()
        gate._trading_state = "active"

    def test_book_and_curve_for_pumpdemo(self):
        book = self.client.get("/api/v1/book", params={"symbol": "PUMPDEMO/SOL"})
        self.assertEqual(book.status_code, 200)
        b = book.json()["data"]
        self.assertEqual(b["symbol"], "PUMPDEMO/SOL")
        self.assertGreater(b["mid"], 0)
        curve = self.client.get("/api/v1/curve", params={"symbol": "PUMPDEMO/SOL"})
        self.assertEqual(curve.status_code, 200)
        c = curve.json()["data"]
        self.assertEqual(c["venue"], "Pump.fun")
        self.assertEqual(c["progress_bps"], 4200)
        self.assertTrue(c["virtual_sol_reserves"])

    def test_pipeline_allow_fill(self):
        r = self.client.post(
            "/api/v1/pipeline/decide-and-fill",
            json={"symbol": "PUMPDEMO/SOL", "side": "buy", "notional": 0.1},
        )
        self.assertEqual(r.status_code, 200)
        data = r.json()["data"]
        self.assertTrue(data["risk"]["allow"])
        self.assertGreaterEqual(len(data["fills"]), 1)
        self.assertNotIn("reject", data)
        self.assertIsNotNone(data["ctx"].get("pump"))
        self.assertEqual(data["ctx"]["pump"]["curve_progress_bps"], 4200)
        f = data["fills"][0]
        for k in ("ts", "price", "qty"):
            self.assertIn(k, f)

    def test_pipeline_wide_spread_deny_no_fill(self):
        r = self.client.post(
            "/api/v1/pipeline/decide-and-fill",
            json={
                "symbol": "PUMPDEMO/SOL",
                "side": "buy",
                "notional": 0.1,
                "spread_bps": 200,
            },
        )
        self.assertEqual(r.status_code, 200)
        data = r.json()["data"]
        self.assertFalse(data["risk"]["allow"])
        self.assertIn("SPREAD_TOO_WIDE", data["risk"]["tags"])
        self.assertEqual(data["fills"], [])
        self.assertIn("reject", data)


if __name__ == "__main__":
    unittest.main()
