"""Exit / gating variant study on synthetic evidence records (no network, no store)."""
from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from app.paper import evidence as ev
from app.paper import exit_study as es
from app.paper import replay
from app.providers.pumpfun_curve_math import INITIAL_VIRTUAL_SOL_RESERVES, INITIAL_VIRTUAL_TOKEN_RESERVES, price_sol

VS0, VT0 = INITIAL_VIRTUAL_SOL_RESERVES, INITIAL_VIRTUAL_TOKEN_RESERVES
K = VS0 * VT0
T0 = 1_700_000_000_000


def _res(mult: float) -> tuple[int, int]:
    vs = int(VS0 * 2.0 * mult ** 0.5)  # spot = mult x the 2x-start curve point
    return vs, K // vs


def _px(mult: float) -> float:
    return price_sol(*_res(mult))


def make_rec(path, *, fill_dt=300, exit_sig_dt=None, mint="M1", signal_ts=T0, features=None, factors=None):
    """``path`` = [(dt_ms from signal, mult, side, sol)]; entry fills at ``fill_dt`` at mult 1.0."""
    rows = [[dt, 1 if side == "buy" else -1, sol, *_res(m)] for dt, m, side, sol in path]
    e_px = _px(1.0)
    qty = 0.03 / e_px
    last = max(r[0] for r in rows)
    return {
        "kind": "paper_trade_evidence",
        "v": 3,
        "market_source": "real",
        "mint": mint,
        "symbol": "SYN/SOL",
        "entry": {"signal_ts": signal_ts, "fill_ts": signal_ts + fill_dt, "fill_price": e_px, "fill_qty": qty,
                  "fill_fee": 0.03 * 0.0125, "decision_price": e_px, "notional_sol": 0.03},
        "exit": {"signal_ts": signal_ts + (exit_sig_dt if exit_sig_dt is not None else fill_dt), "reason": "take_profit"},
        "result": {"net_bps": 0.0},
        "features": features or {},
        "entry_factors": factors or {},
        "tape": {"t0": signal_ts, "from_ts": signal_ts - 30_000, "to_ts": signal_ts + last, "fields": ["dt_ms", "side", "sol", "vs", "vt"], "rows": rows},
    }


class PolicyTests(unittest.TestCase):
    def test_baseline_matches_replay(self):
        path = [(300, 1.0, "buy", 0.1), (900, 1.02, "buy", 0.2), (1_700, 1.07, "buy", 0.3), (2_400, 1.05, "sell", 0.2), (5_000, 1.04, "buy", 0.1)]
        rec = make_rec(path, exit_sig_dt=1_300)
        a = es.simulate_policy(rec, es.ExitPolicy())
        b = replay.simulate(rec, stop=0.05, tp=0.06, exit_delay_ms=300, tick_ms=1000)
        self.assertEqual(a["reason"], b["reason"])
        self.assertEqual(a["reason"], "take_profit")
        self.assertAlmostEqual(a["net_sol"], b["net_sol"], places=12)

    def test_stop_fills_at_next_print_after_latency(self):
        # Check grid anchored on exit signal 1300: checks at 300, 1300, 2300...
        path = [(300, 1.0, "buy", 0.1), (1_000, 0.97, "sell", 0.1), (1_200, 0.80, "sell", 1.0), (1_550, 0.78, "sell", 0.5), (1_700, 0.90, "buy", 0.5)]
        rec = make_rec(path, exit_sig_dt=1_300)
        base = es.simulate_policy(rec, es.ExitPolicy())
        self.assertEqual(base["reason"], "stop_loss")
        self.assertEqual(base["hold_s"], (1_700 - 300) / 1000.0, "fills on first print >= 1300 + 300")
        s3 = es.simulate_policy(rec, es.ExitPolicy(stop=0.03, tick_ms=0))
        self.assertEqual(s3["reason"], "stop_loss")
        self.assertEqual(s3["hold_s"], (1_550 - 300) / 1000.0, "per-print check at 1000 → fill at first print >= 1300")
        self.assertGreater(base["net_sol"], s3["net_sol"], "gapped: the later fill caught the bounce")

    def test_time_stop_trailing_sell_pressure(self):
        flat = [(300, 1.0, "buy", 0.1)] + [(300 + i * 500, 1.0 + 0.001 * i, "buy", 0.05) for i in range(1, 20)]
        rec = make_rec(flat, exit_sig_dt=300)
        ts = es.simulate_policy(rec, es.ExitPolicy(time_stop_s=3, time_stop_min_ret=0.02))
        self.assertEqual(ts["reason"], "time_stop")
        self.assertAlmostEqual(ts["hold_s"], 3.0, delta=0.6)
        run = [(300, 1.0, "buy", 0.1), (1_000, 1.08, "buy", 1), (2_000, 1.20, "buy", 1), (3_000, 1.15, "sell", 1), (4_000, 1.10, "sell", 1), (4_500, 1.09, "buy", 0.1)]
        rec = make_rec(run, exit_sig_dt=300)
        tr = es.simulate_policy(rec, es.ExitPolicy(tp=None, trail_activate=0.06, trail_dist=0.05))
        self.assertEqual(tr["reason"], "trail")  # peak 1.20, 1.10 <= 1.14 at t=4300
        self.assertGreater(tr["net_sol"], 0)
        sp_path = [(300, 1.0, "buy", 0.1), (800, 1.01, "sell", 0.5), (1_100, 1.00, "sell", 0.6), (1_800, 1.0, "buy", 0.1), (2_500, 1.0, "buy", 0.1)]
        rec = make_rec(sp_path, exit_sig_dt=300)
        sp = es.simulate_policy(rec, es.ExitPolicy(sp_window_s=2, sp_ratio=1.0, sp_min_sells=2))
        self.assertEqual(sp["reason"], "sell_pressure")
        self.assertEqual(es.simulate_policy(rec, es.ExitPolicy())["reason"], "tape_end")

    def test_partial_tp_then_break_even(self):
        path = [(300, 1.0, "buy", 0.1), (1_000, 1.05, "buy", 1), (1_500, 1.05, "buy", 0.1), (2_000, 0.99, "sell", 1), (2_500, 0.98, "sell", 1), (3_000, 0.97, "sell", 0.1)]
        rec = make_rec(path, exit_sig_dt=300)
        pol = es.ExitPolicy(partial_frac=0.5, partial_tp=0.04, after_partial_stop=0.0, tp2=0.10)
        out = es.simulate_policy(rec, pol)
        self.assertEqual(out["legs"], 2)
        self.assertEqual(out["reason"], "stop_loss")
        base = es.simulate_policy(rec, es.ExitPolicy())
        self.assertGreater(out["net_sol"], base["net_sol"])

    def test_tape_end_flag(self):
        rec = make_rec([(300, 1.0, "buy", 0.1), (1_000, 1.01, "buy", 0.1)], exit_sig_dt=300)
        out = es.simulate_policy(rec, es.ExitPolicy())
        self.assertEqual((out["reason"], out["flag"]), ("tape_end", "tape_end"))
        self.assertIsNone(es.simulate_policy({"entry": {}, "tape": {}}, es.ExitPolicy()))


class StatsTests(unittest.TestCase):
    def test_stats_and_top3(self):
        rows = [{"status": "ok", "net_bps": b, "net_sol": b / 1e4} for b in (1000, 800, 600, -100, -100, -100)]
        s = es.stats(rows)
        self.assertEqual(s["n"], 6)
        self.assertAlmostEqual(s["mean_bps"], 350.0)
        self.assertAlmostEqual(s["mean_bps_ex_top3"], -100.0)
        self.assertAlmostEqual(s["win_rate"], 0.5)
        self.assertLess(s["ci_lo_bps"], s["mean_bps"])
        self.assertEqual(es.stats([]), {"n": 0})

    def test_time_split_and_gates(self):
        recs = [make_rec([(300, 1.0, "buy", 0.1)], signal_ts=T0 + i, features={"curve": {"progress_bps": i * 100}}) for i in (5, 1, 4, 2, 3, 6)]
        a, b = es.time_split(recs)
        self.assertEqual([r["entry"]["signal_ts"] - T0 for r in a], [1, 2, 3])
        g = es.Gate("p", "features.curve.progress_bps", 200, 500)
        self.assertEqual([g.ok(r) for r in recs], [False, False, True, True, True, False])
        self.assertFalse(es.Gate("b", "entry_factors.bundle_pct", None, 40).ok(recs[0]))
        self.assertTrue(es.Gate("b", "entry_factors.bundle_pct", None, 40, keep_null=True).ok(recs[0]))

    def test_grid_and_cli(self):
        recs = []
        for i in range(12):
            m = 1.10 if i % 2 else 0.90
            recs.append(make_rec([(300, 1.0, "buy", 0.1), (1_000, m, "buy", 0.2), (2_000, m, "sell", 0.1)], signal_ts=T0 + i * 10_000, mint=f"M{i}",
                                 features={"curve": {"progress_bps": 1000 + i}}))
        grid = es.run_grid(recs, es.default_policies()[:3], [es.Gate("all")])
        self.assertEqual(len(grid), 3)
        self.assertEqual(grid[0]["is"]["n"] + grid[0]["oos"]["n"], 12)
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "e.jsonl"
            f.write_text("\n".join(json.dumps(r) for r in recs) + "\n")
            out = Path(d) / "g.json"
            buf = io.StringIO()
            with redirect_stdout(buf):
                self.assertEqual(es.main(["--evidence", str(f), "--json", str(out), "--min-n", "1", "--top", "3"]), 0)
            self.assertIn("baseline", buf.getvalue())
            self.assertEqual(json.loads(out.read_text())["n_records"], 12)


class PostWindowTests(unittest.TestCase):
    def test_post_ms_env(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AUU_EVIDENCE_POST_MS", None)
            self.assertEqual(ev.post_ms(), 90_000)
            os.environ["AUU_EVIDENCE_POST_MS"] = "30000"
            self.assertEqual(ev.post_ms(), 30_000)
            os.environ["AUU_EVIDENCE_POST_MS"] = "1"
            self.assertEqual(ev.post_ms(), 5_000)
            os.environ["AUU_EVIDENCE_POST_MS"] = "bad"
            self.assertEqual(ev.post_ms(), 90_000)


if __name__ == "__main__":
    unittest.main()
