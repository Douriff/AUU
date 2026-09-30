"""Launch tapes (record only), the launch-strategy study and new exit-study rules."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.paper import exit_study as es
from app.paper import launch_study as ls
from app.paper.evidence import EvidenceWriter
from app.paper.launch_tape import LaunchTapeRecorder, launch_tape_enabled
from app.providers.pumpfun_curve_math import INITIAL_VIRTUAL_SOL_RESERVES as VS0, INITIAL_VIRTUAL_TOKEN_RESERVES as VT0
from app.providers.pumpfun_live_paper import PumpfunLivePaperProvider
from tests.test_exit_study import make_rec

K = VS0 * VT0
CREATOR = "CreatorWallet1111111111111111111111111111"
T0 = 1_700_000_000_000


def res_at_vsol(sol: float) -> tuple[int, int]:
    vs = int(sol * 1e9)
    return vs, K // vs


def tape(points, *, creator8=CREATOR[:8], slot=100, name="Good Token", mint="L1", t0=T0, end="max_age"):
    """points: (dt_ms, vsol, side, sol, slot_off, who)."""
    rows = [[dt, 1 if side == "buy" else -1, sol, *res_at_vsol(v), so, who] for dt, v, side, sol, so, who in points]
    return {"kind": "launch_tape", "v": 1, "mint": mint, "creator": CREATOR, "creator8": creator8, "name": name,
            "symbol": "GT", "t0": t0, "created_slot": slot, "end_reason": end, "capped": False, "fields": list(ls.__dict__.get("FIELDS", [])) or [], "rows": rows}


def ramp(start=31.0, stop=112.0, n=60, dt=1000, who="w"):
    pts = [(0, 31.2, "buy", 1.2, 0, CREATOR[:8])]
    for i in range(1, n):
        v = start + (stop - start) * i / (n - 1)
        pts.append((i * dt, v, "buy", 1.0, 2 + i, f"{who}{i % 7}"))
    return pts


class RecorderTests(unittest.TestCase):
    def test_records_from_create_until_drop_or_age(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "lt.jsonl"
            clock = {"now": T0}
            rec = LaunchTapeRecorder(EvidenceWriter(path), max_age_ms=60_000, clock=lambda: clock["now"], background=False)
            with mock.patch("app.paper.launch_tape.get_launch_recorder", return_value=rec):
                p = PumpfunLivePaperProvider(watch_mints="", clock=lambda: clock["now"])
                p.register_watch_mint("LaunchMint111111111111111111111111111111111", base="LNCH", source="logs", creator=CREATOR)
                p.note_create_slot("LaunchMint111111111111111111111111111111111", 500)
                p.note_launch_meta("LaunchMint111111111111111111111111111111111", name="LOUD", symbol="LD")
                p.register_watch_mint("WatchMint1111111111111111111111111111111111", base="WTCH", source="watch")
                base = {"mint": "LaunchMint111111111111111111111111111111111", "virtual_sol_reserves": 31_000_000_000,
                        "virtual_token_reserves": K // 31_000_000_000, "sol_amount": 1.0}
                p.apply_observed_trade(dict(base, side="buy", ts=T0 + 10, trader=CREATOR, slot=500, signature="a"))
                p.apply_observed_trade(dict(base, side="sell", ts=T0 + 900, trader="Other111", slot=502, signature="b"))
                self.assertEqual(rec.status()["open"], 1, "only logs-sourced mints are taped")
                clock["now"] = T0 + 61_000
                rec.expire()
            lines = [json.loads(x) for x in path.read_text().splitlines()]
            self.assertEqual(len(lines), 1)
            r = lines[0]
            self.assertEqual((r["end_reason"], r["name"], r["created_slot"], r["creator8"]), ("max_age", "LOUD", 500, CREATOR[:8]))
            self.assertEqual([row[5] for row in r["rows"]], [0, 2])
            self.assertEqual(r["rows"][0][6], CREATOR[:8])
            self.assertEqual(r["rows"][1][1], -1)

    def test_drop_and_graduation_end_tapes(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "lt.jsonl"
            rec = LaunchTapeRecorder(EvidenceWriter(path), clock=lambda: T0, background=False)
            rec.on_register("A", creator=CREATOR, source="logs", registered_ts=T0)
            rec.on_register("B", creator=CREATOR, source="logs", registered_ts=T0)
            rec.on_drop("A")
            rec.on_print("B", ts=T0 + 5, side="buy", sol=80.0, vs=115_000_000_000, vt=279_900_000_000_000, slot=None, trader="x")
            ends = sorted(json.loads(x)["end_reason"] for x in path.read_text().splitlines())
            self.assertEqual(ends, ["evicted", "graduated"])

    def test_off_under_tests_by_default(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AUU_LAUNCH_TAPE", None)
            self.assertFalse(launch_tape_enabled())
            os.environ["AUU_LAUNCH_TAPE"] = "on"
            self.assertTrue(launch_tape_enabled())


class LaunchStudyTests(unittest.TestCase):
    def test_speed_entry_exits_before_graduation(self):
        rec = tape(ramp())
        pol = ls.LaunchPolicy(name="h2", vsol=50.0, exit_vsol=105.0, timeout_s=120, latency_ms=1000)
        out = ls.simulate_launch(rec, pol)
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["reason"], "pre_graduation")
        self.assertGreater(out["net_sol"], 0, "rising curve pays")
        self.assertIsNone(ls.simulate_launch(rec, ls.LaunchPolicy(name="x", vsol=50.0, max_trades=5)), "too slow")

    def test_no_fill_after_graduation(self):
        pts = [(0, 31.2, "buy", 1.2, 0, CREATOR[:8]), (1000, 60, "buy", 5, 3, "a"), (2500, 70, "buy", 5, 5, "b"), (3000, 116.0, "buy", 40, 6, "c")]
        rec = tape(pts, end="graduated")
        rec["rows"][-1][4] = VT0 - 793_100_000_000_000  # real tokens 0 → graduated
        out = ls.simulate_launch(rec, ls.LaunchPolicy(name="h2", vsol=50.0, exit_vsol=None, timeout_s=120, latency_ms=1000))
        self.assertEqual(out["flag"], "graduated_no_curve_fill")

    def test_creator_buy_entry_and_filters(self):
        pts = [(0, 31.2, "buy", 1.2, 0, CREATOR[:8])] + [(1000 * i, 31.2 + i, "buy", 0.5, i + 1, f"u{i}") for i in range(1, 40)]
        rec = tape(pts)
        feats = ls.launch_features(rec)
        self.assertAlmostEqual(feats["creator_buy_sol"], 1.2)
        self.assertGreater(feats["creator_buy_pct"], 2.0)
        self.assertEqual(feats["launch_block_foreign_buys"], 0)
        h4 = ls.LaunchPolicy(name="h4", entry="creator_buy", min_creator_sol=1.0, exit_vsol=None, tp=0.30, stop=0.30, timeout_s=120, latency_ms=1000)
        out = ls.simulate_launch(rec, h4)
        self.assertEqual((out["status"], out["reason"]), ("ok", "take_profit"))
        q = ls.LaunchPolicy(name="h1", entry="creator_buy", tp=0.3, stop=0.3, exit_vsol=None, creator_quality=True)
        self.assertEqual(ls.simulate_launch(rec, q)["status"], "ok")
        self.assertEqual(ls.simulate_launch(rec, q, prior_launches=1)["why"], "repeat_creator")
        self.assertEqual(ls.simulate_launch(tape(pts, name="PUMP IT"), q)["why"], "all_caps")
        bundled = tape(pts[:1] + [(10, 33, "buy", 2, 0, "sniper1")] + pts[1:])
        self.assertEqual(ls.simulate_launch(bundled, replace_pol(h4, veto_bundle=True))["why"], "bundled_launch")
        bots = {f"u{i}" for i in range(1, 40)}
        self.assertEqual(ls.simulate_launch(rec, replace_pol(h4, entry="speed", vsol=50.0, veto_bot_share=0.5), bots=bots)["why"], "bot_share")

    def test_bot_wallets_and_run(self):
        recs = [tape(ramp(), mint=f"M{i}", t0=T0 + i * 1000) for i in range(10)]
        for r in recs:
            r["rows"][1][5] = 1
            r["rows"][1][6] = "botbot01"
        self.assertIn("botbot01", ls.bot_wallets(recs))
        res = ls.run(recs, ls.default_launch_policies()[:2], split=0.7)
        self.assertEqual((res["n_fit"], res["n_validate"]), (7, 3))
        self.assertEqual(res["table"][0]["fit"]["n"], 7)
        self.assertIn("boot_lo_bps", res["table"][0]["fit"])


def replace_pol(p, **kw):
    from dataclasses import replace

    return replace(p, **kw)


class ExitStudyExtraTests(unittest.TestCase):
    def test_dump_exit_on_outlier_print(self):
        pre = [(-10_000 + i * 500, 1.0 + (0.002 if i % 2 else -0.002), "buy", 0.1) for i in range(15)]
        post = [(300, 1.0, "buy", 0.1), (800, 1.0, "buy", 0.1), (1_100, 0.965, "sell", 3.0), (1_500, 0.96, "sell", 0.1), (2_500, 0.95, "sell", 0.1), (3_500, 1.0, "buy", 0.1)]
        rec = make_rec(pre + post, exit_sig_dt=300)
        out = es.simulate_policy(rec, es.ExitPolicy(dump_k=4.0))
        self.assertEqual(out["reason"], "dump")
        self.assertAlmostEqual(out["hold_s"], (1_500 - 300) / 1000.0)

    def test_creator_sell_needs_who_column(self):
        rec = make_rec([(300, 1.0, "buy", 0.1), (900, 1.0, "sell", 1.0), (1_300, 0.99, "buy", 0.1), (2_000, 0.9, "buy", 0.1)], exit_sig_dt=300)
        rec["features"] = {"token": {"creator": CREATOR}}
        self.assertEqual(es.simulate_policy(rec, es.ExitPolicy(creator_sell=True))["status"], "not_applicable")
        rec["tape"]["fields"].append("who")
        for row, who in zip(rec["tape"]["rows"], ["x", CREATOR[:8], "y", "z"]):
            row.append(who)
        out = es.simulate_policy(rec, es.ExitPolicy(creator_sell=True))
        self.assertEqual(out["reason"], "creator_sell")
        self.assertAlmostEqual(out["hold_s"], 1.0)

    def test_latency_and_priority_fee(self):
        path = [(300, 1.0, "buy", 0.1), (1_000, 1.05, "buy", 0.1), (1_900, 1.07, "buy", 0.1), (2_500, 1.08, "buy", 0.1), (4_000, 1.08, "buy", 0.1)]
        rec = make_rec(path, exit_sig_dt=300)
        a = es.simulate_policy(rec, es.ExitPolicy())
        b = es.simulate_policy(rec, es.ExitPolicy(prio_fee_sol=0.0001))
        self.assertAlmostEqual(a["net_sol"] - b["net_sol"], 0.0002, places=9)
        c = es.simulate_policy(rec, es.ExitPolicy(entry_delay_ms=1_000))
        self.assertLess(c["net_sol"], a["net_sol"], "later entry pays the run-up")

    def test_bootstrap_and_ex_top1(self):
        lo, hi = es.bootstrap_ci([1.0, 2.0, 3.0, 4.0])
        self.assertTrue(1.0 <= lo <= hi <= 4.0)
        s = es.stats([{"status": "ok", "net_bps": float(x), "net_sol": x / 1e4} for x in range(100)], bootstrap=True)
        self.assertAlmostEqual(s["mean_bps_ex_top1pct"], sum(range(99)) / 99)
        self.assertIn("boot_lo_bps", s)
        a, b = es.time_split([make_rec([(300, 1.0, "buy", 0.1)], signal_ts=T0 + i) for i in range(10)], 0.7)
        self.assertEqual((len(a), len(b)), (7, 3))


if __name__ == "__main__":
    unittest.main()
