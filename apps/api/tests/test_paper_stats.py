"""Paper performance stats + autopaper toggle / live-refuse (paper only)."""
from __future__ import annotations

import os
import unittest

from app.models.contracts import Fill
from app.paper.broker import reset_paper_broker
from app.paper.guard import live_execution_blocked
from app.paper.ledger import (
    PaperLedger,
    monte_carlo,
    reset_paper_ledger,
    summarize,
)
from app.providers import reset_provider
from app.risk.gate import reset_risk_gate
from app.strategies.pump_paper_v1 import PumpPaperEngine, PumpPaperParams, reset_engine


class LedgerUnitTests(unittest.TestCase):
    def test_round_trip_win_rate(self):
        led = PaperLedger()
        led.record_fill(
            "PUMPDEMO/SOL",
            Fill(ts=1, price=1.0, qty=10.0, fee=0.0, tag="paper:in"),
        )
        closed = led.record_fill(
            "PUMPDEMO/SOL",
            Fill(ts=2, price=1.1, qty=-10.0, fee=0.0, tag="paper:out"),
        )
        self.assertEqual(len(closed), 1)
        self.assertAlmostEqual(closed[0].pnl_pct, 0.1)
        stats = summarize(led.closed, stop_loss_pct=0.12, day_loss_pct=0.05, n_paths=200, seed=1)
        self.assertEqual(stats["trade_count"], 1)
        self.assertEqual(stats["wins"], 1)
        self.assertEqual(stats["losses"], 0)
        self.assertAlmostEqual(stats["win_rate"], 1.0)
        self.assertAlmostEqual(stats["expectancy_pnl_pct"], 10.0)
        self.assertFalse(stats["empty"])
        self.assertIn("simulation from paper history", stats["disclaimer"])

    def test_loss_and_drawdown(self):
        led = PaperLedger()
        led.record_fill("A/SOL", Fill(ts=1, price=10.0, qty=1.0, fee=0.0))
        led.record_fill("A/SOL", Fill(ts=2, price=8.0, qty=-1.0, fee=0.0))
        led.record_fill("A/SOL", Fill(ts=3, price=8.0, qty=1.0, fee=0.0))
        led.record_fill("A/SOL", Fill(ts=4, price=9.0, qty=-1.0, fee=0.0))
        stats = summarize(led.closed, n_paths=50, seed=7)
        self.assertEqual(stats["wins"], 1)
        self.assertEqual(stats["losses"], 1)
        self.assertAlmostEqual(stats["win_rate"], 0.5)
        self.assertGreater(stats["max_drawdown_pct"], 0)
        mc = stats["monte_carlo"]
        self.assertEqual(mc["n_paths"], 50)
        self.assertIsNotNone(mc["p_equity_positive"])
        self.assertIn("not a promise", mc["label"])

    def test_monte_carlo_seed_stable(self):
        rets = [0.1, -0.05, 0.02, -0.12, 0.08]
        a = monte_carlo(rets, n_paths=300, seed=42)
        b = monte_carlo(rets, n_paths=300, seed=42)
        self.assertEqual(a, b)
        self.assertGreaterEqual(a["p_equity_positive"], 0)
        self.assertLessEqual(a["p_equity_positive"], 1)

    def test_empty_summarize(self):
        stats = summarize([])
        self.assertTrue(stats["empty"])
        self.assertIsNone(stats["win_rate"])
        self.assertIsNone(stats["monte_carlo"])
        self.assertEqual(stats["trade_count"], 0)


class GuardTests(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("LIVE_TRADING", None)
        os.environ.pop("WALLET_PRIVATE_KEY", None)

    def test_default_paper_ok(self):
        blocked, why = live_execution_blocked()
        self.assertFalse(blocked)
        self.assertEqual(why, "")

    def test_live_env_refuses(self):
        os.environ["LIVE_TRADING"] = "1"
        blocked, why = live_execution_blocked()
        self.assertTrue(blocked)
        self.assertIn("LIVE_TRADING", why)
        self.assertIn("paper-only", why)


class StatsApiTests(unittest.TestCase):
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
        reset_provider()
        reset_engine()
        reset_risk_gate()
        reset_paper_broker()
        reset_paper_ledger()

    def setUp(self):
        reset_paper_ledger()
        reset_risk_gate()
        reset_paper_broker()
        r = self.client.put(
            "/api/v1/strategy/pump-paper-v1", json={"auto_paper_orders": False}
        )
        self.assertEqual(r.status_code, 200)

    def test_empty_performance(self):
        r = self.client.get("/api/v1/stats/paper-performance")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["ok"])
        data = body["data"]
        self.assertTrue(data["empty"])
        self.assertEqual(data["trade_count"], 0)
        self.assertIsNone(data["win_rate"])
        self.assertTrue(data["liveDisabled"])
        self.assertEqual(data["mode"], "paper")
        self.assertIn("simulation from paper history", data["disclaimer"])
        self.assertFalse(data["auto_paper_orders"])
        self.assertFalse(data["strategy_autopaper"])

    def test_round_trip_updates_stats(self):
        buy = self.client.post(
            "/api/v1/pipeline/decide-and-fill",
            json={"symbol": "PUMPDEMO/SOL", "side": "buy", "notional": 0.05},
        )
        self.assertEqual(buy.status_code, 200)
        self.assertGreaterEqual(len(buy.json()["data"]["fills"]), 1)
        sell = self.client.post(
            "/api/v1/pipeline/decide-and-fill",
            json={"symbol": "PUMPDEMO/SOL", "side": "sell", "notional": 0.05},
        )
        self.assertEqual(sell.status_code, 200)
        self.assertGreaterEqual(len(sell.json()["data"]["fills"]), 1)
        r = self.client.get("/api/v1/stats/paper-performance")
        data = r.json()["data"]
        self.assertGreaterEqual(data["trade_count"], 1)
        self.assertFalse(data["empty"])
        self.assertIsNotNone(data["win_rate"])
        self.assertIn("monte_carlo", data)
        mc = data["monte_carlo"]
        self.assertEqual(mc["n_paths"], 1000)
        self.assertIn("not a promise", mc["label"])
        self.assertIsNotNone(mc["p_equity_positive"])
        self.assertIn("p_hit_day_loss", mc)

    def test_health_strategy_autopaper_alias_and_toggle(self):
        h = self.client.get("/api/v1/health")
        data = h.json()["data"]
        self.assertIn("auto_paper_orders", data)
        self.assertIn("strategy_autopaper", data)
        self.assertEqual(data["auto_paper_orders"], data["strategy_autopaper"])
        self.assertTrue(data["liveDisabled"])
        off = data["strategy_autopaper"]
        r = self.client.put(
            "/api/v1/strategy/pump-paper-v1", json={"strategy_autopaper": not off}
        )
        self.assertEqual(r.status_code, 200)
        payload = r.json()["data"]
        self.assertEqual(payload["strategy_autopaper"], (not off))
        self.assertEqual(payload["auto_paper_orders"], (not off))
        h2 = self.client.get("/api/v1/health").json()["data"]
        self.assertEqual(h2["strategy_autopaper"], (not off))
        self.assertEqual(h2["auto_paper_orders"], (not off))
        # toggle back without restart
        self.client.put(
            "/api/v1/strategy/pump-paper-v1", json={"auto_paper_orders": False}
        )
        h3 = self.client.get("/api/v1/health").json()["data"]
        self.assertFalse(h3["auto_paper_orders"])
        self.assertFalse(h3["strategy_autopaper"])


class AutopaperLiveRefuseTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        os.environ["DATA_PROVIDER"] = "pumpfun_paper"
        os.environ["PUMP_PAPER_LOOP"] = "0"
        os.environ["PUMPFUN_DISCOVERY"] = "off"
        os.environ["LIVE_TRADING"] = "1"
        reset_provider()
        reset_engine()
        reset_risk_gate()
        reset_paper_broker()
        reset_paper_ledger()

    def tearDown(self):
        os.environ.pop("LIVE_TRADING", None)
        reset_engine()
        reset_risk_gate()
        reset_paper_broker()
        reset_paper_ledger()
        os.environ["DATA_PROVIDER"] = "mock"
        reset_provider()

    def _seed_buy_tape(self, symbol: str = "PUMPDEMO/SOL") -> None:
        from app.providers import get_provider
        import time

        p = get_provider()
        now = int(time.time() * 1000)
        rows = []
        for i in range(12):
            rows.append(
                {
                    "mint": "DemoMintPump11111111111111111111111111111",
                    "symbol": symbol,
                    "ts": now - i * 1_000,
                    "side": "buy" if i < 10 else "sell",
                    "price": 2.8e-5,
                    "qty": 1000,
                    "sol_amount": 0.8 if i < 10 else 0.05,
                    "phase": "curve",
                }
            )
        p._trades[symbol] = rows  # type: ignore[attr-defined]

    async def test_live_env_blocks_auto_fill(self):
        self._seed_buy_tape()
        engine = PumpPaperEngine(
            PumpPaperParams(auto_paper_orders=True, max_notional_sol=0.1)
        )
        await engine.tick()
        self.assertEqual(engine.positions, {})
        dec = engine.last_decisions()
        self.assertTrue(any(d["action"] == "refuse" for d in dec))
        self.assertTrue(any("LIVE" in d["reason"] or "LIVE" in (d["notes"] or "") for d in dec))


if __name__ == "__main__":
    unittest.main()
