"""Pre-registered signal candidates: frozen hash, long-only, forward-only trades/NAV, isolated tables."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.paper import auu_signal_shadow as sh
from app.strategies import auu_signal_candidates as C

DAY = 86_400_000


def _series(n=120, t0=C.REG_TS - 80 * DAY):
    closes = [100.0 * (1.012 ** i) if i < 90 else 100.0 * (1.012 ** 90) * (0.985 ** (i - 90)) for i in range(n)]
    vols = [1000.0 + (400 if i % 4 == 0 else 0) for i in range(n)]
    ts = [t0 + i * DAY for i in range(n)]
    return closes, vols, ts


class CandidateTests(unittest.TestCase):
    def test_hash_frozen(self):
        # registered values (docs/signal-candidates.md); editing params must change the id, not these
        self.assertEqual(C.PARAMS_HASH, {k: C.params_hash(k) for k in ("cand_A", "cand_B", "cand_A_ls", "cand_B_ls")})
        self.assertEqual(C.PARAMS_HASH["cand_A_ls"], "3587d21c0a98d883")
        self.assertEqual(C.PARAMS_HASH["cand_B_ls"], "1ee21f59d7664055")
        self.assertEqual(C.PARAMS_HASH["cand_A"], "6f2f7dea6d308afe")
        self.assertEqual(C.PARAMS_HASH["cand_B"], "25fad2f2e60c7325")

    def test_long_only_and_shape(self):
        closes, vols, ts = _series()
        for cid in ("cand_A", "cand_B"):
            rows = C.compute_candidate(cid, closes=closes, volumes=vols, ts=ts, funding=[0.0001] * len(ts))
            self.assertEqual(len(rows), len(ts))
            self.assertTrue(all(r["side"] in ("long", "flat") for r in rows))
            self.assertIn("long", [r["side"] for r in rows])
        b = C.compute_candidate("cand_B", closes=closes, volumes=vols, ts=ts, funding=[0.0001] * len(ts))
        self.assertTrue(all(r["factors"]["funding"] is None for r in b))

    def test_long_short_symmetric(self):
        closes, vols, ts = _series()
        down = [c for c in reversed(closes)]
        rows = C.compute_candidate("cand_B_ls", closes=down, volumes=vols, ts=ts)
        self.assertIn("short", [r["side"] for r in rows])
        for a, b in zip(rows, rows[1:]):
            if b["side"] == "short" and a["side"] != "short":
                self.assertLessEqual(b["score"], -0.25)
            if a["side"] == "short" and b["side"] == "flat":
                self.assertGreater(b["score"], -0.10)

    def test_nav_carry_in_counts_flat(self):
        ts = [C.REG_TS + (i - 5) * DAY for i in range(12)]
        rows = [{"asof_ts": t, "side": "long"} for t in ts]
        opens = {t: 100.0 + i for i, t in enumerate(ts)}
        opens[ts[-1] + DAY] = 200.0
        self.assertEqual(sh.forward_nav({"X": (rows, opens, [])}, bar_ms=DAY, since_ts=C.REG_TS), [])
        rows2 = rows[:7] + [{"asof_ts": t, "side": "flat"} for t in ts[7:9]] + [{"asof_ts": t, "side": "long"} for t in ts[9:]]
        nav = sh.forward_nav({"X": (rows2, opens, [])}, bar_ms=DAY, since_ts=C.REG_TS)
        self.assertTrue(nav and nav[0][0] == ts[7] + DAY and nav[0][3] == 0)

    def test_forward_only_trades_and_nav(self):
        closes, vols, ts = _series()
        rows = C.compute_candidate("cand_B", closes=closes, volumes=vols, ts=ts)
        opens = {t: c for t, c in zip(ts, closes)}
        tr = C.hypothetical_trades(rows, opens, bar_ms=DAY)
        self.assertTrue(all(t["entry_ts"] > C.REG_TS for t in tr))
        nav = sh.forward_nav({"X": (rows, opens, [])}, bar_ms=DAY, since_ts=C.REG_TS)
        self.assertTrue(all(d > C.REG_TS for d, *_ in nav))

    def test_tables_isolated_from_v0_bars(self):
        with tempfile.TemporaryDirectory() as d:
            st = sh.SignalShadowStore(Path(d) / "s.sqlite")
            closes, vols, ts = _series()
            rows = C.compute_candidate("cand_A", closes=closes, volumes=vols, ts=ts)
            st.upsert_cand_bars("cand_A", C.PARAMS_HASH["cand_A"], "BTC", "1d", rows)
            st.replace_cand_trades("cand_A", "BTC", [])
            st.replace_cand_nav("cand_A", [(1, 0.0, 1.0, 0, 0, 1)])
            self.assertEqual(st.count(), 0)  # v0 bars table untouched
            st.close()


if __name__ == "__main__":
    unittest.main()
