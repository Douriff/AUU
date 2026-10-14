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
        self.assertEqual(C.PARAMS_HASH, {"cand_A": C.params_hash("cand_A"), "cand_B": C.params_hash("cand_B")})
        self.assertEqual(C.PARAMS_HASH["cand_A"], "6f2f7dea6d308afe")
        self.assertEqual(C.PARAMS_HASH["cand_B"], "25fad2f2e60c7325")

    def test_long_only_and_shape(self):
        closes, vols, ts = _series()
        for cid in C.CANDIDATES:
            rows = C.compute_candidate(cid, closes=closes, volumes=vols, ts=ts, funding=[0.0001] * len(ts))
            self.assertEqual(len(rows), len(ts))
            self.assertTrue(all(r["side"] in ("long", "flat") for r in rows))
            self.assertIn("long", [r["side"] for r in rows])
        b = C.compute_candidate("cand_B", closes=closes, volumes=vols, ts=ts, funding=[0.0001] * len(ts))
        self.assertTrue(all(r["factors"]["funding"] is None for r in b))

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
