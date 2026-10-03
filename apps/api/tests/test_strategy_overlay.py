"""Strategy chart overlay (P1-5): read-only, same targets()/signal as the runner, fills per coin."""
from __future__ import annotations

import unittest

from app.paper import strategy_overlay
from app.strategies.indicators import pct_change
from app.strategies.trend_tsmom import TrendTSMOM
from tests.test_strategy_runner import Clock, _Base


class OverlayTests(_Base):
    def setUp(self):
        super().setUp()
        strategy_overlay.clear_cache()
        self.clock = Clock(self.panel.days[150])
        self.r = self.runner(self.clock)
        self.run_days(self.r, self.clock, 150, 200)

    def test_series_matches_strategy_targets_and_fills_are_per_coin(self):
        ov = strategy_overlay.build(self.r, "eth/usdt", days=120)
        self.assertEqual(ov["symbol"], "ETH")
        self.assertTrue(ov["inUniverse"])
        self.assertEqual(ov["lookbacks"], [20, 60, 120])
        due = self.r.due_day()
        self.assertEqual(ov["asOfDay"], due)
        self.assertEqual(len(ov["series"]), 120)
        self.assertEqual(ov["series"][-1]["ts"], due)
        p = self.panel_fn(due)
        want = TrendTSMOM().targets(p)["ETH"]
        m60 = pct_change(p.spot_close["ETH"], 60)
        for row in ov["series"]:
            i = p.days.index(row["ts"])
            self.assertEqual(row["target"], want[i])
            self.assertEqual(row["mom"][1], m60[i])
            if row["signal"] is not None and row["signal"] == 0:
                self.assertEqual(row["target"], 0.0)
        self.assertEqual(ov["revised"], [])  # same data as when the runner ran
        self.assertEqual(sum(1 for x in ov["series"] if x["recorded"] is not None), 51)
        fills = ov["fills"]
        self.assertTrue(fills)
        led = [f for f in self.r.ledger.fills(5000) if f["coin"] == "ETH"]
        self.assertEqual(len(fills), len(led))
        self.assertEqual([f["day"] for f in fills], sorted(f["day"] for f in fills))
        for f in fills:  # every rebalance trades to that day's target weight (what the marker shows)
            row = next(x for x in ov["series"] if x["ts"] == f["day"])
            self.assertAlmostEqual(f["wTo"], row["target"], places=12)

    def test_revised_days_are_flagged(self):
        # the data under past days changes after they ran (e.g. a late backfill): recompute differs
        for i in range(len(self.panel.days)):
            self.panel.spot_close["ETH"][i] *= 1 + 0.2 * ((i * 7919) % 13 - 6) / 6
        strategy_overlay.clear_cache()
        ov = strategy_overlay.build(self.r, "ETH", days=120)
        self.assertTrue(ov["revised"])
        for ts in ov["revised"]:
            row = next(x for x in ov["series"] if x["ts"] == ts)
            self.assertNotAlmostEqual(row["recorded"], row["target"], places=9)

    def test_read_only_and_unknown_coin(self):
        before = (len(self.r.ledger.runs()), len(self.r.ledger.fills(5000)))
        ov = strategy_overlay.build(self.r, "DOGE")
        self.assertFalse(ov["inUniverse"])
        self.assertEqual(ov["series"], [])
        self.assertEqual(ov["fills"], [])
        strategy_overlay.build(self.r, "BTC")
        self.assertEqual(before, (len(self.r.ledger.runs()), len(self.r.ledger.fills(5000))))


class OverlayRouteTests(unittest.TestCase):
    def test_login_required(self):
        import os
        from unittest.mock import patch

        from fastapi.testclient import TestClient

        from app.main import create_app

        with patch.dict(os.environ, {"AUU_AUTH": "on", "AUU_UPTIME": "off", "AUU_RECON": "off", "AUU_ALERTS": "off"}):
            with TestClient(create_app(legacy=False)) as c:
                self.assertEqual(c.get("/api/v1/mainstream/strategy/overlay?symbol=BTC").status_code, 401)


if __name__ == "__main__":
    unittest.main()
