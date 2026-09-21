"""Paper performance stats + autopaper toggle / live-refuse (paper only)."""
from __future__ import annotations

import os
import unittest

from app.models.contracts import Fill
from app.paper.broker import reset_paper_broker
from app.paper.guard import live_execution_blocked
from app.paper.ledger import (
    EQUITY_0,
    MIN_SAMPLE_OK,
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
            Fill(ts=1, price=1.0, qty=10.0, fee=0.0, tag="paper-trade-ui"),
        )
        closed = led.record_fill(
            "PUMPDEMO/SOL",
            Fill(ts=2, price=1.1, qty=-10.0, fee=0.0, tag="paper-trade-ui"),
        )
        self.assertEqual(len(closed), 1)
        rt = closed[0]
        self.assertAlmostEqual(rt.pnl_pct, 0.1)
        self.assertEqual(rt.source, "manual")
        self.assertEqual(rt.strategy_id, "manual-paper")
        self.assertTrue(rt.id)
        stats = summarize(led.closed)
        self.assertEqual(stats["n_trades"], 1)
        self.assertEqual(stats["wins"], 1)
        self.assertEqual(stats["losses"], 0)
        self.assertAlmostEqual(stats["win_rate"], 1.0)
        self.assertEqual(stats["win_rate"], stats["wins"] / stats["n_trades"])
        self.assertAlmostEqual(stats["expectancy"], rt.pnl)
        self.assertFalse(stats["empty"])
        self.assertFalse(stats["sample_ok"])
        self.assertIsNone(stats["monte_carlo"])
        self.assertEqual(stats["equity"][0]["equity"], EQUITY_0)
        self.assertAlmostEqual(stats["equity"][-1]["equity"], EQUITY_0 + rt.pnl)

    def test_signal_source_from_autopaper_tag(self):
        led = PaperLedger()
        led.record_fill("A/SOL", Fill(ts=1, price=1.0, qty=2.0, tag="paper:pump-paper-v1"))
        closed = led.record_fill(
            "A/SOL", Fill(ts=2, price=0.9, qty=-2.0, tag="paper:pump-paper-v1:flat"), reason="take_profit"
        )
        self.assertEqual(closed[0].source, "signal")
        self.assertEqual(closed[0].strategy_id, "pump-paper-v1")
        self.assertIn("TAKE_PROFIT", closed[0].tags)

    def test_loss_and_drawdown(self):
        led = PaperLedger()
        led.record_fill("A/SOL", Fill(ts=1, price=10.0, qty=1.0, fee=0.0))
        led.record_fill("A/SOL", Fill(ts=2, price=8.0, qty=-1.0, fee=0.0))
        led.record_fill("A/SOL", Fill(ts=3, price=8.0, qty=1.0, fee=0.0))
        led.record_fill("A/SOL", Fill(ts=4, price=9.0, qty=-1.0, fee=0.0))
        stats = summarize(led.closed, n_paths=50, seed=7, mc=True)
        self.assertEqual(stats["wins"], 1)
        self.assertEqual(stats["losses"], 1)
        self.assertAlmostEqual(stats["win_rate"], 0.5)
        self.assertGreater(stats["max_drawdown_pct"], 0)
        mc = stats["monte_carlo"]
        self.assertFalse(mc["sample_ok"])
        self.assertEqual(mc["note"], "样本不足")
        self.assertEqual(mc["method"], "shuffle")
        self.assertIn("p50_pnl", mc)

    def test_monte_carlo_seed_stable_and_sample_ok(self):
        pnls = [0.1] * MIN_SAMPLE_OK
        a = monte_carlo(pnls, n_paths=200, seed=42)
        b = monte_carlo(pnls, n_paths=200, seed=42)
        self.assertEqual(a, b)
        self.assertTrue(a["sample_ok"])
        self.assertIn("p50_pnl", a)
        self.assertIn("p50_dd", a)

    def test_empty_summarize(self):
        stats = summarize([])
        self.assertTrue(stats["empty"])
        self.assertIsNone(stats["win_rate"])
        self.assertIsNone(stats["monte_carlo"])
        self.assertEqual(stats["n_trades"], 0)
        self.assertFalse(stats["sample_ok"])


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
        r = self.client.get("/api/v1/strategy/pump-paper-v1/stats")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["ok"])
        data = body["data"]
        self.assertTrue(data["empty"])
        self.assertEqual(data["n_trades"], 0)
        self.assertIsNone(data["win_rate"])
        self.assertTrue(data["liveDisabled"])
        self.assertFalse(data["mc"])
        self.assertIsNone(data["monte_carlo"])
        self.assertFalse(data["sample_ok"])
        self.assertFalse(data["strategy_autopaper"])
        alias = self.client.get("/api/v1/stats/paper-performance")
        self.assertTrue(alias.json()["ok"])

    def test_round_trip_updates_stats_and_reset(self):
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
        r = self.client.get("/api/v1/strategy/pump-paper-v1/stats")
        data = r.json()["data"]
        self.assertGreaterEqual(data["n_trades"], 1)
        self.assertFalse(data["empty"])
        self.assertIsNotNone(data["win_rate"])
        self.assertIn("expectancy", data)
        self.assertFalse(data["mc"])
        self.assertIsNone(data["monte_carlo"])
        rt = data["journal"][0]
        for k in ("id", "strategy_id", "symbol", "entry_ts", "exit_ts", "pnl", "tags"):
            self.assertIn(k, rt)
        mc_r = self.client.get("/api/v1/strategy/pump-paper-v1/stats", params={"mc": "1"})
        mc = mc_r.json()["data"]["monte_carlo"]
        self.assertFalse(mc["sample_ok"])
        self.assertEqual(mc["note"], "样本不足")
        self.assertEqual(mc["method"], "shuffle")
        reset = self.client.post("/api/v1/strategy/pump-paper-v1/stats/reset")
        self.assertEqual(reset.status_code, 200)
        self.assertEqual(reset.json()["data"]["n_trades"], 0)

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
        self.client.put(
            "/api/v1/strategy/pump-paper-v1", json={"auto_paper_orders": False}
        )


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


if __name__ == "__main__":
    unittest.main()
