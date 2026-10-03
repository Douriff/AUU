"""Performance report: metrics, monthly, drawdown duration, per-coin attribution reconciles with the ledger."""
from __future__ import annotations

import unittest

from app.backtest.panel import DAY_MS
from app.paper import perf_report as pr
from tests.test_strategy_risk import Clock, _Base

D0 = 1_735_689_600_000  # 2025-01-01


class MetricTests(unittest.TestCase):
    def test_metrics_and_drawdown_duration(self):
        days = [D0 + k * DAY_MS for k in range(6)]
        rets = [0.01, -0.02, -0.01, 0.005, 0.04, -0.01]
        m = pr.metrics(days, rets)
        self.assertEqual(m["days"], 6)
        self.assertAlmostEqual(m["winRate"], 0.5)
        self.assertEqual(m["longestDrawdownDays"], 3)  # days 2-4 under the day-1 peak, day 5 recovers
        self.assertEqual(m["currentDrawdownDays"], 1)
        self.assertLess(m["maxDrawdown"], 0)
        self.assertIsNotNone(m["sortino"])
        self.assertIsNotNone(m["calmar"])
        self.assertTrue(m["shortSample"])
        self.assertEqual(pr.metrics([], [])["days"], 0)

    def test_monthly_compounds(self):
        feb = D0 + 31 * DAY_MS
        mo = pr.monthly([D0, D0 + DAY_MS, feb], [0.1, 0.1, -0.05])
        self.assertEqual([x["month"] for x in mo], ["2025-01", "2025-02"])
        self.assertAlmostEqual(mo[0]["ret"], 0.21)
        self.assertEqual(mo[0]["days"], 2)


class AttributionTests(_Base):
    def _report(self, r):
        return pr.build(r, funding_fn=self.funding)

    def test_reconciles_without_risk_caps(self):
        clock = Clock(0)
        self.at(clock, 150, 0)
        r = self.runner(clock, limits=None)
        for i in range(150, 181):
            self.at(clock, i, 0)
            r.tick()
        rep = self._report(r)
        a = rep["attribution"]
        self.assertGreater(len(a["coins"]), 2)
        self.assertTrue(a["fundingByCoin"])
        self.assertLess(abs(a["residualUsd"]), 1e-6 * 10_000, a)
        self.assertEqual(rep["metrics"]["days"], 31)
        self.assertEqual(len(rep["drawdown"]), 31)
        self.assertEqual(rep["goNoGo"]["verdict"], "pending")
        self.assertIsNotNone(rep["ci"])

    def test_reconciles_across_intraday_adjustment_segments(self):
        r, clock, e, w = self.ready_book()
        g = self.gross(w)
        self.shock = {c: 1 - 0.06 / g for c in w}  # intraday -6% -> halve adjustment
        self.run_hourly(r, clock, e + 1, e + 1)
        self.shock = {}
        self.run_hourly(r, clock, e + 2, e + 3)
        rep = self._report(r)
        a = rep["attribution"]
        self.assertGreaterEqual(a["segmentsFromAdjustments"], 1)
        self.assertLess(abs(a["residualUsd"]), 1e-3 * 10_000, a)

    def test_without_funding_fn_falls_back_to_ledger_total(self):
        r, clock, e, w = self.ready_book()
        a = pr.build(r)["attribution"]
        self.assertFalse(a["fundingByCoin"])
        self.assertLess(abs(a["residualUsd"]), 1e-6 * 10_000, a)


if __name__ == "__main__":
    unittest.main()
