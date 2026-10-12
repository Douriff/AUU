"""auu_signal_v0: formula contract, hysteresis, shadow isolation from strategy ledger."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.paper import auu_signal_shadow as sh
from app.strategies.auu_signal_v0 import VERSION, AuuSignalV0, align_funding_to_bars


class FormulaTests(unittest.TestCase):
    def test_version_frozen_and_score_in_unit_interval(self):
        self.assertEqual(VERSION, "auu_signal_v0")
        n = 80
        closes = [100.0 * (1.01 ** i) for i in range(n)]  # steady uptrend
        volumes = [1000.0 + (50 if i % 3 == 0 else 0) for i in range(n)]
        ts = [1_700_000_000_000 + i * 86_400_000 for i in range(n)]
        rows = AuuSignalV0().compute(closes=closes, volumes=volumes, ts=ts)
        self.assertEqual(len(rows), n)
        self.assertEqual(rows[-1]["asof_ts"], ts[-1])
        for r in rows:
            self.assertGreaterEqual(r["score"], -1.0)
            self.assertLessEqual(r["score"], 1.0)
            self.assertIn(r["side"], ("long", "short", "flat"))
            self.assertIn(r["action"], ("enter", "hold", "exit", "none"))
            self.assertIsInstance(r["reason_tags"], list)
        # uptrend should eventually be long with an enter
        sides = [r["side"] for r in rows]
        self.assertIn("long", sides)
        actions = [r["action"] for r in rows]
        self.assertIn("enter", actions)
        # hold only allowed on the last bar
        for r in rows[:-1]:
            self.assertNotEqual(r["action"], "hold")

    def test_not_naked_ma_cross_identity(self):
        # flat price → near-zero score / flat (no fake MA-cross churn)
        n = 60
        closes = [100.0] * n
        volumes = [1000.0] * n
        ts = [i * 86_400_000 for i in range(n)]
        rows = AuuSignalV0().compute(closes=closes, volumes=volumes, ts=ts)
        self.assertTrue(all(r["side"] == "flat" for r in rows))
        self.assertTrue(all(abs(r["score"]) < 1e-9 for r in rows))

    def test_risk_gate_forces_flat_without_entering_score(self):
        n = 60
        closes = [100.0 * (1.02 ** i) for i in range(n)]
        volumes = [2000.0] * n
        ts = [i * 86_400_000 for i in range(n)]
        gates = [None] * n
        gates[-1] = ["news_halt"]
        rows = AuuSignalV0().compute(closes=closes, volumes=volumes, ts=ts, risk_gates=gates)
        self.assertEqual(rows[-1]["side"], "flat")
        self.assertEqual(rows[-1]["score"], 0.0)
        self.assertIn("risk_gate", rows[-1]["reason_tags"])
        self.assertIn("news_halt", rows[-1]["reason_tags"])

    def test_funding_alignment_no_lookahead(self):
        bar_ms = 86_400_000
        ts = [0, bar_ms, 2 * bar_ms]
        # settlement at bar0 close visible to bar0; settlement after bar1 close invisible to bar1
        fr = [(bar_ms, 0.001), (2 * bar_ms + 1, 0.002)]
        aligned = align_funding_to_bars(ts, fr, bar_ms=bar_ms)
        self.assertEqual(aligned[0], 0.001)
        self.assertEqual(aligned[1], 0.001)  # still last known; next settle is after bar1 close
        self.assertEqual(aligned[2], 0.002)  # bar2 close = 3*bar_ms sees 2*bar_ms+1
        # settlement strictly after a bar close must not leak into that bar
        late = align_funding_to_bars([0], [(1, 0.5)], bar_ms=bar_ms)
        self.assertEqual(late[0], 0.5)  # 1 <= bar_ms
        too_late = align_funding_to_bars([0], [(bar_ms + 1, 0.5)], bar_ms=bar_ms)
        self.assertIsNone(too_late[0])


class ShadowIsolationTests(unittest.TestCase):
    def test_shadow_sqlite_separate_and_upsert(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = sh.SignalShadowStore(Path(tmp) / "auu_signal_shadow.sqlite")
            candles = [
                {"ts": 1_700_000_000_000 + i * 86_400_000, "open": 100 + i, "high": 101 + i, "low": 99 + i, "close": 100 + i * 0.5, "volume": 1000 + i}
                for i in range(80)
            ]
            payload = sh.compute_and_record("BTC", candles, tf="1d", funding_rows=[], store=store, now_ms=candles[-1]["ts"] + 86_400_000)
            self.assertEqual(payload["version"], VERSION)
            self.assertTrue(payload["experimental"])
            self.assertGreater(payload["shadowWritten"], 0)
            got = store.series("BTC", "1d", limit=10)
            self.assertTrue(got)
            self.assertEqual(got[-1]["asof_ts"], payload["latest"]["asof_ts"])
            # strategy ledger path must not be created by this module
            self.assertFalse((Path(tmp) / "mainstream_strategy.sqlite").exists())
            store.close()


if __name__ == "__main__":
    unittest.main()
