"""Expected band (P0-3) and run provenance (P0-4)."""
from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from app import alerts as al
from app.backtest import expected_band as gen
from app.paper import expected_band as eb
from app.paper import strategy_runner as sr
from tests.test_alerts import Outbox


def tiny_band(H=40):
    return {"name": "t", "version": 1, "registered": {"seed": 1, "block": 20, "n_paths": 10}, "source": {"returns_sha256": "ab" * 32},
            "cum_p05": [-0.01 * (k + 1) for k in range(H)], "cum_p50": [0.0] * H, "cum_p95": [0.01 * (k + 1) for k in range(H)],
            "mdd_p05": [-0.02 * (k + 1) for k in range(H)], "mdd_p50": [0.0] * H}


class BandTests(unittest.TestCase):
    def test_bootstrap_is_deterministic_and_ordered(self):
        r = [0.001 * ((i * 7) % 11 - 5) for i in range(200)]
        a, b = gen.bootstrap_band(r, n_paths=200, horizon=50), gen.bootstrap_band(r, n_paths=200, horizon=50)
        self.assertEqual(a, b)
        for k in range(50):
            self.assertLessEqual(a["cum_p05"][k], a["cum_p50"][k])
            self.assertLessEqual(a["cum_p50"][k], a["cum_p95"][k])
            self.assertLessEqual(a["mdd_p05"][k], 0.0)

    def test_committed_band_is_registered(self):
        d = eb.load_band()
        self.assertIsNotNone(d)
        self.assertEqual(d["registered"]["seed"], gen.SEED)
        self.assertEqual(d["registered"]["block"], gen.BLOCK)
        self.assertEqual(len(d["cum_p05"]), gen.HORIZON)
        self.assertEqual(d["source"]["n_days"], 630)

    def test_evaluate_status(self):
        b = tiny_band()
        self.assertEqual(eb.evaluate([], band=b)["status"], "no_data")
        self.assertEqual(eb.evaluate([0.0, 0.005], band=b)["status"], "inside")
        self.assertEqual(eb.evaluate([-0.03], band=b)["status"], "below")
        self.assertEqual(eb.evaluate([0.03], band=b)["status"], "above")
        # cum inside (-1.0%... ) but drawdown deeper than p05 (-6% at N=3)
        self.assertEqual(eb.evaluate([0.04, -0.07, 0.01], band=b)["status"], "dd_breach")
        self.assertEqual(eb.evaluate([0.0] * 41, band=b)["status"], "beyond_horizon")
        self.assertIn("不是 Go 判定", eb.evaluate([0.0], band=b)["note"])


class _FakeLedger:
    pass


class BandAlertTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.t = [1_800_000_000_000]
        self.box = Outbox()
        self.c = al.AlertCenter(Path(self.tmp.name) / "a.sqlite", send=self.box, configured=lambda: True, now_ms=lambda: self.t[0])
        self.ev = {"status": "inside"}

        class R:
            ledger = _FakeLedger()

            def status(self):
                return {}

        self.mon = al.AlertMonitor(self.c, runner_fn=lambda: None, freshness_fn=lambda: {}, band_fn=lambda led: self.ev)
        self.r = R()

    def tearDown(self):
        self.c.close()
        self.tmp.cleanup()

    def _outside(self, st, day):
        self.ev = {**eb.evaluate([-0.03], band=tiny_band()), "status": st, "day": day}

    def test_alert_on_entry_then_weekly(self):
        self.assertEqual(self.mon._band(self.r), [])
        self._outside("below", "2026-10-05")
        self.assertEqual(self.mon._band(self.r), ["sent"])
        self.assertIn("低于预期区间", self.box.sent[-1][1] + self.box.sent[-1][2])
        self._outside("below", "2026-10-06")
        self.t[0] += 86_400_000
        self.assertEqual(self.mon._band(self.r), [])  # same state within a week
        self.t[0] += 7 * 86_400_000
        self.assertEqual(self.mon._band(self.r), ["sent"])
        self._outside("dd_breach", "2026-10-14")
        self.assertEqual(self.mon._band(self.r), ["sent"])  # new state
        self.ev = {"status": "inside"}
        self.mon._band(self.r)
        self.assertIsNone(self.c.get("band_alert"))
        self.assertEqual(len(self.box.sent), 3)


class ProvenanceTests(unittest.TestCase):
    def test_old_ledger_migrates_and_provenance_is_stable(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "s.sqlite"
            db = sqlite3.connect(p)
            db.executescript(sr._SCHEMA)
            db.close()
            led = sr.StrategyLedger(p)
            cols = {r[1] for r in led._db.execute("PRAGMA table_info(runs)")}
            self.assertTrue({"git_commit", "params_sha", "cost_model_sha"} <= cols)
            r = sr.StrategyRunner(led, panel_fn=lambda d: None, ready_fn=lambda d: (False, "x"))
            a, b = r.provenance(), r.provenance()
            self.assertEqual(a, b)
            self.assertEqual(len(a["params_sha"]), 64)
            r2 = sr.StrategyRunner(led, panel_fn=lambda d: None, ready_fn=lambda d: (False, "x"), start_nav=5_000.0)
            self.assertNotEqual(r2.provenance()["params_sha"], a["params_sha"])
            self.assertEqual(r2.provenance()["cost_model_sha"], a["cost_model_sha"])
            v = r.version_summary()
            self.assertEqual(v["unversionedRuns"], 0)
            led.close()



from tests.test_strategy_risk import Clock, _Base  # noqa: E402


class RunnerProvenanceTests(_Base):
    def test_each_run_row_carries_commit_and_hashes_and_band_in_summary(self):
        clock = Clock(0)
        self.at(clock, 150, 0)
        led = sr.StrategyLedger(Path(self.tmp.name) / "p.sqlite", now_ms=clock)
        self.ledgers.append(led)
        r = sr.StrategyRunner(led, panel_fn=self.panel_fn, ready_fn=lambda d: (True, ""), start_nav=10_000.0, now_ms=clock)
        for i in range(150, 156):
            self.at(clock, i, 0)
            r.tick()
        runs = led.runs()
        self.assertGreater(len(runs), 3)
        want = r.provenance()
        for x in runs:
            self.assertEqual((x["git_commit"], x["params_sha"], x["cost_model_sha"]),
                             (want["git_commit"], want["params_sha"], want["cost_model_sha"]))
        s = r.summary()
        self.assertEqual(s["expectedBand"]["n"], len(runs))
        self.assertIn(s["expectedBand"]["status"], ("inside", "below", "above", "dd_breach"))
        self.assertEqual(s["version"]["lastRun"]["params_sha"], want["params_sha"])
        self.assertFalse(s["version"]["changedSinceLastRun"])


if __name__ == "__main__":
    unittest.main()
