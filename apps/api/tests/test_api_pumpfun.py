"""API smoke: DATA_PROVIDER=pumpfun_paper hello / snapshot / paper Fill."""
from __future__ import annotations

import os
import unittest

os.environ.setdefault("DATA_PROVIDER", "pumpfun_paper")

from fastapi.testclient import TestClient

from app.providers import reset_provider


class PumpfunApiTests(unittest.TestCase):
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

    def test_health_provider(self):
        r = self.client.get("/api/v1/health")
        self.assertEqual(r.status_code, 200)
        data = r.json()["data"]
        self.assertEqual(data["provider"], "pumpfun_paper")
        self.assertEqual(data["mode"], "paper")
        self.assertEqual(data["venue"], "Pump.fun")
        self.assertIn("pumpfun_paper", data["marketProviderOptions"])
        self.assertEqual(data["dataSourceOptions"], ["mock", "paper", "pumpfun_paper"])

    def test_symbols_and_snapshot(self):
        r = self.client.get("/api/v1/symbols")
        self.assertEqual(r.status_code, 200)
        syms = r.json()["data"]
        self.assertTrue(any(s["symbol"] == "PUMPDEMO/SOL" for s in syms))
        self.assertTrue(all(s["kind"] == "pumpfun_curve" for s in syms))
        snap = self.client.get("/api/v1/pumpfun/snapshot", params={"symbol": "PUMPDEMO/SOL"})
        self.assertEqual(snap.status_code, 200)
        body = snap.json()["data"]
        self.assertEqual(body["progress_bps"], 4200)
        self.assertTrue(body["virtual_sol_reserves"])
        self.assertTrue(body["real_token_reserves"])

    def test_candles(self):
        r = self.client.get("/api/v1/candles", params={"symbol": "PUMPDEMO/SOL", "interval": "1m"})
        self.assertEqual(r.status_code, 200)
        bars = r.json()["data"]
        self.assertGreater(len(bars), 10)

    def test_paper_order_still_fills(self):
        ctx = {
            "symbol": "PUMPDEMO/SOL",
            "ts": 1_700_000_000_000,
            "tick": {"mid": 0.00003},
            "liquidity": {"spread_bps": 20, "adv_usd": 100000},
            "pump": {
                "curve_progress_bps": 4200,
                "virtual_sol_reserves": "30000000000",
                "virtual_token_reserves": "1073000000000000",
                "real_sol_reserves": "0",
                "real_token_reserves": "793100000000000",
                "creator_fee_bps": 0,
                "complete": False,
                "migrated": False,
            },
        }
        # Quote notional is SOL on the curve; 0.1 SOL stays under impact_cap_bps.
        pre = self.client.post(
            "/api/v1/risk/pre-order",
            json={
                "ctx": ctx,
                "signal": {"side": "long", "strength": 0.5, "reason": "test"},
                "size": {"target_notional": 0.1, "max_slippage_bps": 500},
            },
        )
        self.assertEqual(pre.status_code, 200)
        risk = pre.json()["data"]
        self.assertTrue(risk["allow"])
        order = self.client.post(
            "/api/v1/paper/orders",
            json={
                "ctx": ctx,
                "intent": {
                    "side": "buy",
                    "order_type": "market",
                    "qty_or_notional": 0.1,
                    "client_tag": "paper",
                    "max_slippage_bps": 500,
                },
                "risk": risk,
            },
        )
        self.assertEqual(order.status_code, 200)
        fills = order.json()["data"]["fills"]
        self.assertGreaterEqual(len(fills), 1)
        f = fills[0]
        for k in ("ts", "price", "qty", "fee", "slippage_bps", "tag"):
            self.assertIn(k, f)

    def test_ws_hello_lists_pumpfun_paper(self):
        with self.client.websocket_connect("/api/v1/ws") as ws:
            hello = ws.receive_json()
            self.assertEqual(hello["type"], "hello")
            self.assertEqual(hello["version"], 1)
            self.assertIn("pumpfun_paper", hello["providers"])
            self.assertEqual(hello.get("orderMode"), "paper")
            self.assertEqual(hello.get("venue"), "Pump.fun")
            ws.send_json({"type": "subscribe", "channel": "trades", "symbol": "PUMPDEMO/SOL"})
            # subscribed + at least one pumpfun_curve or trade
            seen_curve = False
            seen_trade = False
            for _ in range(8):
                msg = ws.receive_json()
                if msg.get("type") == "pumpfun_curve":
                    seen_curve = True
                    self.assertIn("progress_bps", msg["payload"])
                if msg.get("type") == "trade":
                    seen_trade = True
                    self.assertIn(msg["payload"]["side"], ("buy", "sell"))
                if seen_curve:
                    break
            self.assertTrue(seen_curve or seen_trade)


if __name__ == "__main__":
    unittest.main()
