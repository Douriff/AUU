"""M2 backtest engine + shared trend strategy (synthetic data, stdlib only)."""
from __future__ import annotations

import csv
import io
import math
import os
import tempfile
import unittest
import zipfile
from pathlib import Path

from app.backtest import engine, research, stats
from app.backtest.costs import CostModel
from app.backtest.panel import DAY_MS, Panel, build_panel, day_ms, load_binance_archive, load_store, ms_day
from app.strategies import REGISTRY, TrendTSMOM, TrendTSMOMParams
from app.strategies.indicators import pct_change, rolling_all_present, rolling_std

D0 = day_ms("2020-01-01")


def make_panel(prices: dict[str, list], funding: dict[str, list] | None = None, start=D0) -> Panel:
    n = len(next(iter(prices.values())))
    days = [start + i * DAY_MS for i in range(n)]
    sc = {c: list(v) for c, v in prices.items()}
    fu = {c: list((funding or {}).get(c, [0.0] * n)) for c in prices}
    return Panel(days, list(prices), sc, {c: list(v) for c, v in prices.items()}, fu, dict(sc), dict(sc), "synthetic")


def trend(n, start=100.0, drift=0.004, wiggle=0.02, phase=0.0):
    return [start * math.exp(drift * i + wiggle * math.sin(i / 3.0 + phase)) for i in range(n)]


class IndicatorTests(unittest.TestCase):
    def test_pct_change_and_rolling_std_match_definitions(self):
        x = [1.0, 2.0, None, 4.0, 5.0, 6.0]
        got = pct_change(x)
        self.assertEqual([g is None for g in got], [True, False, True, True, False, False])
        for g, e in zip(got, [None, 1.0, None, None, 0.25, 0.2]):
            if e is not None:
                self.assertAlmostEqual(g, e)
        s = rolling_std([1.0, 2.0, 3.0, 4.0, None, 6.0], 3)
        self.assertEqual(s[:2], [None, None])
        self.assertAlmostEqual(s[2], 1.0)
        self.assertAlmostEqual(s[3], 1.0)
        self.assertIsNone(s[4])  # window with a gap
        self.assertEqual(rolling_all_present([1, 1, None, 1, 1], [1, 1, 1, 1, 1], n=2), [False, True, False, False, True])


class EngineTests(unittest.TestCase):
    def test_timing_cost_and_funding(self):
        # +10% on day 2; target 0.5 decided at the close of day 1 earns day 2.
        p = make_panel({"BTC": [100, 100, 110, 110]}, funding={"BTC": [0, 0, 0.001, 0]})
        T = {"BTC": [0.0, 0.5, 0.5, 0.5]}
        cost = CostModel(taker=0.001, slippage={"BTC": 0.0})
        r = engine.run(p, T, start=ms_day(p.days[0]), end=ms_day(p.days[-1]), cost=cost)
        self.assertAlmostEqual(r.returns[0], 0.0)
        self.assertAlmostEqual(r.returns[1], -0.5 * 0.001)  # buy 0.5 at the close: cost only
        pnl2 = 0.5 * 0.10 - 0.5 * 0.001  # price move minus funding (long pays)
        w_after = 0.5 * 1.1 / (1 + pnl2)
        self.assertAlmostEqual(r.returns[2], pnl2 - abs(0.5 - w_after) * 0.001 * (abs(0.5 - w_after) > max(0.1, 0.01)), places=12)
        self.assertAlmostEqual(r.funding[2], 0.5 * 0.001)

    def test_band_skips_small_drift_and_exit_always_trades(self):
        p = make_panel({"BTC": [100, 100, 101, 101, 101]})
        T = {"BTC": [0.0, 0.5, 0.5, 0.5, 0.0]}
        r = engine.run(p, T, start=ms_day(p.days[0]), end=ms_day(p.days[-1]), cost=CostModel(taker=0.0, slippage={}, slippage_default=0.0))
        self.assertAlmostEqual(r.turnover[1], 0.5)
        self.assertEqual(r.turnover[2], 0.0)  # 1% drift is inside the 20% band
        self.assertGreater(r.turnover[4], 0.49)  # exit to flat

    def test_cost_multiplier_scales_costs(self):
        p = make_panel({"ETH": trend(40)})
        T = {"ETH": [0.0] + [0.3 if i % 5 else 0.0 for i in range(1, 40)]}
        a = engine.run(p, T, start=ms_day(p.days[0]), end=ms_day(p.days[-1]), cost=CostModel())
        b = engine.run(p, T, start=ms_day(p.days[0]), end=ms_day(p.days[-1]), cost=CostModel().scaled(2))
        self.assertAlmostEqual(sum(b.costs), 2 * sum(a.costs))
        self.assertAlmostEqual(CostModel().per_side("ETH"), 0.0006)
        self.assertAlmostEqual(CostModel().per_side("ATOM"), 0.0008)


class StatsTests(unittest.TestCase):
    def test_summary_fields(self):
        days = [D0 + i * DAY_MS for i in range(120)]
        rnd = __import__("random").Random(3)
        r = [rnd.gauss(0.002, 0.02) for _ in range(120)]
        s = stats.summarize(days, r, label="x")
        self.assertAlmostEqual(s["ann_mean"], sum(r) / 120 * 365)
        self.assertLess(s["ci_lo"], s["ann_mean"])
        self.assertGreater(s["ci_hi"], s["ann_mean"])
        self.assertEqual(len(s["best3m"]), 3)
        self.assertLess(s["ex_best3m_ann"], s["ann_mean"] + 1e-12)
        self.assertAlmostEqual(s["excess_vs_tbill"], s["ann_mean"] - 0.0399)
        self.assertEqual(stats.summarize(days, r, label="x"), s)  # seeded: reproducible

    def test_mdd_percentile_split(self):
        self.assertAlmostEqual(stats.max_drawdown([0.1, -0.5, 0.2]), -0.5)
        self.assertAlmostEqual(stats.percentile([1, 2, 3, 4], 50), 2.5)
        self.assertAlmostEqual(stats.percentile(list(range(101)), 2.5), 2.5)
        self.assertEqual(stats.split_holdout(2099, 0.3), 1469)

    def test_numpy_style_rng_is_accepted(self):
        class Fixed:
            def integers(self, lo, hi, size):
                return [0] * size

        r = [float(i) for i in range(60)]
        lo, hi = stats.boot_ci(r, rng=Fixed(), n=5)
        self.assertAlmostEqual(lo, hi)


class StrategyTests(unittest.TestCase):
    def setUp(self):
        n = 400
        self.panel = make_panel({"BTC": trend(n), "ETH": trend(n, drift=-0.004, phase=1.0), "SOL": trend(n, drift=0.002, phase=2.0)})

    def test_long_only_targets(self):
        T = TrendTSMOM().targets(self.panel)
        self.assertTrue(all(w == 0 for w in T["BTC"][:120]))  # warm-up: 90d eligibility + 120d lookback
        self.assertGreater(T["BTC"][-1], 0)
        self.assertEqual(T["ETH"][-1], 0.0)  # downtrend: flat, never short
        self.assertTrue(all(w >= 0 for c in T for w in T[c]))
        ls = TrendTSMOM(TrendTSMOMParams(long_only=False)).targets(self.panel)
        self.assertLess(ls["ETH"][-1], 0)
        # per-coin size <= cap / N_eligible
        self.assertTrue(all(abs(w) <= 2.0 / 3 + 1e-12 for c in ls for w in ls[c]))

    def test_decide_equals_targets_row_and_lookahead_check(self):
        s = TrendTSMOM()
        full = s.targets(self.panel)
        for n in (150, 260, 399):
            d = s.decide(self.panel.truncate(n + 1))
            for c in self.panel.coins:
                self.assertEqual(d[c], full[c][n])
        self.assertTrue(research.lookahead_check(s, self.panel, samples=4)["ok"])

    def test_lookahead_check_catches_a_peeking_strategy(self):
        class Peeking(TrendTSMOM):
            def targets(self, panel):
                out = {c: [0.0] * len(panel) for c in panel.coins}
                for c in panel.coins:
                    px = panel.perp_close[c]
                    for t in range(len(panel) - 1):
                        out[c][t] = 0.5 if px[t + 1] > px[t] else 0.0  # uses tomorrow
                return out

        self.assertFalse(research.lookahead_check(Peeking(), self.panel, samples=6)["ok"])

    def test_registry(self):
        self.assertIs(REGISTRY["trend_tsmom_v1"], TrendTSMOM)


class ResearchProtocolTests(unittest.TestCase):
    def test_full_report(self):
        n = 900
        p = make_panel({"BTC": trend(n), "ETH": trend(n, drift=0.001, wiggle=0.05, phase=1.0)})
        rep = research.run_research(TrendTSMOM(), p, start=ms_day(p.days[200]), end=ms_day(p.days[-1]), walk_forward_from=2021, lookahead_samples=2)
        sr = rep["strategy_result"]
        k = stats.split_holdout(n - 200, 0.3)
        self.assertEqual(sr["train"]["days"], k)
        self.assertEqual(sr["hold"]["days"], n - 200 - k)
        self.assertIn("cost_x2", rep["sensitivity"])
        self.assertIn("lag_1d", rep["sensitivity"])
        self.assertIn("BTC buy&hold", rep["benchmarks"])
        self.assertEqual(rep["benchmarks"]["T-bill"]["ann"], 0.0399)
        self.assertTrue(rep["lookahead"]["ok"])
        self.assertIn("oos", rep["walk_forward"])
        self.assertLessEqual(rep["sensitivity"]["cost_x2"]["ann_mean"], sr["hold"]["ann_mean"] + 1e-12)

    def test_walk_forward_picks_on_prior_data_only(self):
        # Up in 2020, down hard in 2021, up in 2022. Grid: always long ("A") vs always short ("B").
        px, v = [], 100.0
        days = [day_ms("2020-01-01") + i * DAY_MS for i in range(3 * 365 + 1)]
        for i, d in enumerate(days):
            y = int(ms_day(d)[:4])
            v *= math.exp({2020: 0.002, 2021: -0.006}.get(y, 0.002) + 0.01 * math.sin(i))
            px.append(v)
        p = make_panel({"BTC": px})
        n = len(px)
        tg = lambda g: {"BTC": [0.5 if g == "A" else -0.5] * n}  # noqa: E731
        wf = research.walk_forward(p, tg, ["A", "B"], start="2020-01-01", end=ms_day(days[-1]), first_year=2021,
                                   cost=CostModel(), band=0.2, eligible={"BTC": [True] * n})
        self.assertEqual(wf["picks"][2021], "A")  # chosen on 2020 only (up year)
        self.assertEqual(wf["picks"][2022], "B")  # 2020+2021 data: the crash dominates
        self.assertTrue(all(int(ms_day(d)[:4]) >= 2021 for d in wf["days"]))


def _zip(path: Path, rows: list[list], header: list[str] | None = None):
    buf = io.StringIO()
    w = csv.writer(buf)
    if header:
        w.writerow(header)
    w.writerows(rows)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(path.stem + ".csv", buf.getvalue())


class LoaderTests(unittest.TestCase):
    def test_binance_archive_layout(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            d = [day_ms("2020-01-01") + i * DAY_MS for i in range(4)]
            k = lambda t, c, us=False: [t * 1000 if us else t, c, c + 1, c - 1, c, 1, t + 1, 10]  # noqa: E731
            _zip(root / "spot_BTC_2020-01.zip", [k(d[0], 100), k(d[1], 101)])
            _zip(root / "spot_BTC_2020-02.zip", [k(d[2], 102, us=True)], header=["open_time", "o", "h", "l", "c", "v", "ct", "qv"])
            _zip(root / "perp_BTC_2020-01.zip", [k(t, 200 + i) for i, t in enumerate(d)])
            _zip(root / "fund_BTC_2020-01.zip", [[d[1] + 1, 8, 0.0001], [d[1] + 8 * 3600_000, 8, 0.0002]], header=["calc_time", "h", "r"])
            p = load_binance_archive(root, ["BTC", "ETH"], start="2020-01-01", end="2020-01-04")
        self.assertEqual(p.coins, ["BTC"])
        self.assertEqual(p.spot_close["BTC"], [100, 101, 102, 203])  # day 4: spot missing -> perp print
        self.assertEqual(p.spot_high["BTC"][2], 103)
        self.assertEqual(p.perp_close["BTC"], [200, 201, 202, 203])
        self.assertAlmostEqual(p.funding["BTC"][1], 0.0003)
        self.assertEqual(p.funding["BTC"][0], 0.0)

    def test_store_panel(self):
        from app.marketdata.mainstream.store import MarketStore

        with tempfile.TemporaryDirectory() as tmp:
            st = MarketStore(Path(tmp) / "m.sqlite")
            base = day_ms("2025-01-01")
            st.upsert_candles("okx", "BTC", "1d", [[base + i * DAY_MS, 1, 2, 0.5, 100 + i, 5] for i in range(5)])
            st.upsert_funding("okx", "BTC", [(base + DAY_MS, 0.0001), (base + DAY_MS + 8 * 3600_000, 0.0002)])
            p = load_store(st, "okx", ["BTC", "ETH"])
            st.close()
        self.assertEqual(p.coins, ["BTC"])
        self.assertEqual(len(p), 4)  # newest (forming) day dropped
        self.assertEqual(p.perp_close["BTC"], p.spot_close["BTC"])
        self.assertAlmostEqual(p.funding["BTC"][1], 0.0003)


class AcceptanceGate(unittest.TestCase):
    """Runs the real reproduction when the research archive is available (AUU_BT_ARCHIVE=dir)."""

    @unittest.skipUnless(os.getenv("AUU_BT_ARCHIVE"), "set AUU_BT_ARCHIVE to the Binance archive dir")
    def test_reproduces_research(self):
        from app.backtest import acceptance

        self.assertEqual(acceptance.main(["--archive", os.environ["AUU_BT_ARCHIVE"]]), 0)

    def test_no_heavy_dependencies(self):
        root = Path(__file__).resolve().parents[1] / "app"
        for path in list((root / "backtest").glob("*.py")) + list((root / "strategies").glob("*.py")):
            text = path.read_text(encoding="utf-8")
            for mod in ("import pandas", "import numpy\n", "from numpy", "import vectorbt", "import freqtrade", "from freqtrade"):
                if path.name == "acceptance.py" and mod.startswith("import numpy"):
                    continue
                self.assertNotIn(mod, text, f"{path.name}: {mod}")


if __name__ == "__main__":
    unittest.main()
