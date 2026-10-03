"""Risk caps on the strategy's automatic paper trading (leverage report §3.2)."""
from __future__ import annotations

import json
import math
import random
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.backtest import engine
from app.backtest.costs import CostModel
from app.backtest.panel import DAY_MS, Panel, day_ms, ms_day
from app.paper import strategy_risk as risk
from app.paper import strategy_runner as sr
from app.paper.strategy_risk import HOUR_MS, RiskLimits
from app.strategies.trend_tsmom import TrendTSMOM

D0 = day_ms("2025-01-01")
COINS10 = ["BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "ADA", "LINK", "AVAX", "TRX"]


def make_panel(n=300, seed=11, coins=COINS10, vol=0.03) -> Panel:
    rnd = random.Random(seed)
    days = [D0 + i * DAY_MS for i in range(n)]
    sc, fu = {}, {}
    for k, c in enumerate(coins):
        px, out = 50.0 * (k + 1), []
        drift = (0.002, 0.0015, 0.001, 0.0025, 0.0018)[k % 5]
        for i in range(n):
            px *= math.exp(drift + rnd.gauss(0, vol))
            out.append(px)
        sc[c] = out
        fu[c] = [0.00009 for _ in range(n)]  # 3 settlements of 0.003% a day
    return Panel(days, list(coins), sc, {c: list(v) for c, v in sc.items()}, fu, source="synthetic")


class Clock:
    def __init__(self, t: int):
        self.t = t

    def __call__(self):
        return self.t


class _Base(unittest.TestCase):
    coins = COINS10
    vol = 0.03

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "s.sqlite"
        self.panel = make_panel(coins=self.coins, vol=self.vol)
        self.shock: dict[str, float] = {}  # coin -> multiplicative shock on intraday marks
        self.stale: set[str] = set()
        self.extra_funding: dict[tuple[str, int], float] = {}
        self.health_ok = True
        self.ledgers = []

    def tearDown(self):
        for l in self.ledgers:
            l.close()
        self.tmp.cleanup()

    # --- market fakes: hourly path = geometric interpolation between daily closes
    def path_px(self, c: str, t: int) -> float:
        i = (t - D0) // DAY_MS  # bar inside UTC day i: between close(i-1) and close(i)
        px = self.panel.perp_close[c]
        a, b = px[i - 1], px[i]
        x = ((t - D0) % DAY_MS) / DAY_MS
        return a * (b / a) ** x

    def marks(self, coins, bar):
        out = {}
        for c in coins:
            ts = bar - 5 * HOUR_MS if c in self.stale else bar
            out[c] = (ts, self.path_px(c, ts + HOUR_MS) * self.shock.get(c, 1.0))
        return {"prices": out, "ok": self.health_ok, "reason": "" if self.health_ok else "exchange down"}

    def funding(self, c, lo, hi):
        out = []
        d = lo // DAY_MS * DAY_MS
        while d < hi:
            i = (d - D0) // DAY_MS
            for h in (0, 8, 16):
                ts = d + h * HOUR_MS
                if lo <= ts < hi:
                    out.append((ts, self.extra_funding.get((c, ts), self.panel.funding[c][i] / 3)))
            d += DAY_MS
        return out

    def panel_fn(self, day):
        return self.panel.truncate(self.panel.days.index(day) + 1)

    def runner(self, clock, limits=RiskLimits(), **kw):
        led = sr.StrategyLedger(self.path, now_ms=clock)
        self.ledgers.append(led)
        return sr.StrategyRunner(led, panel_fn=self.panel_fn, ready_fn=lambda d: (True, ""), start_nav=10_000.0, now_ms=clock,
                                 risk_limits=limits, marks_fn=self.marks, funding_fn=self.funding, **kw)

    def at(self, clock, i, hour, minute=10):
        """Clock to `hour`:`minute` UTC on the day after panel day i closed."""
        clock.t = self.panel.days[i] + DAY_MS + hour * HOUR_MS + minute * 60_000

    def run_hourly(self, r, clock, first, last):
        for i in range(first, last + 1):
            for h in range(24):
                self.at(clock, i, h)
                r.tick()

    def events(self, r, kind=None):
        ev = [dict(x) for x in r.ledger.risk_events(1000)]
        return [e for e in ev if kind is None or e["kind"] == kind]

    def ready_book(self, s=150, e=170):
        clock = Clock(0)
        self.at(clock, s, 0)
        r = self.runner(clock)
        for i in range(s, e + 1):
            self.at(clock, i, 0)
            r.tick()
        last = r.ledger.last_run()
        w = json.loads(last["weights"])
        self.assertGreaterEqual(len(w), 3, "synthetic tape should hold positions")
        return r, clock, e, w

    def gross(self, w):
        return sum(abs(x) for x in w.values())


class NoTriggerTests(_Base):
    def test_hourly_monitor_without_triggers_matches_the_backtest_engine(self):
        s, e = 130, 175
        clock = Clock(0)
        self.at(clock, s, 0)
        r = self.runner(clock)
        self.run_hourly(r, clock, s, e)
        runs = r.ledger.runs()
        self.assertEqual(len(runs), e - s + 1)
        strat = TrendTSMOM()
        bt = engine.run(self.panel, strat.targets(self.panel), start=ms_day(self.panel.days[s]), end=ms_day(self.panel.days[e]),
                        cost=CostModel(), band=0.2, eligible=strat.eligibility(self.panel))
        self.assertGreater(sum(bt.turnover), 0.3)
        for got, want in zip([x["ret"] for x in runs], bt.returns):
            self.assertAlmostEqual(got, want, places=12)
        self.assertEqual(self.events(r), [])
        self.assertEqual(r.ledger.adjustments(), [])
        self.assertTrue(all(x["risk_targets"] is None for x in runs))
        mark = r.ledger.risk_get("last_mark")
        self.assertIsNotNone(mark)  # the monitor did run every hour
        self.assertLess(abs(mark["dayRet"]), 0.03)


class IntradayLossTests(_Base):
    def shock_to(self, w, day_ret):
        g = self.gross(w)
        f = 1 + day_ret / g
        self.shock = {c: f for c in w}

    def test_minus3_stops_new_positions_for_the_rest_of_the_day(self):
        r, clock, e, w = self.ready_book()
        self.shock_to(w, -0.04)
        self.at(clock, e, 6)
        r.tick()
        ev = self.events(r, "day_stop_new")
        self.assertEqual(len(ev), 1)
        self.assertLess(ev[0]["value"], -0.03)
        self.assertEqual(r.ledger.adjustments(), [])  # -3% only blocks new exposure
        self.at(clock, e, 7)
        r.tick()
        self.assertEqual(len(self.events(r, "day_stop_new")), 1)  # logged once per day
        self.shock = {}
        self.at(clock, e + 1, 0)  # close of the shocked day: no increase over held weights
        r.tick()
        run = r.ledger.last_run()
        self.assertEqual(run["day"], self.panel.days[e + 1])
        held = json.loads(r.ledger.runs()[-2]["weights"])
        for c, x in json.loads(run["weights"]).items():
            self.assertIn(c, held)  # no new coin
        self.assertTrue(r.ledger.risk_get(f"day:{self.panel.days[e + 1]}")["stop_new"])
        rt = json.loads(run["risk_targets"] or run["targets"])
        drifted_cap = {c: abs(x) for c, x in held.items()}
        for c, x in rt.items():
            self.assertLessEqual(abs(x), drifted_cap.get(c, 0.0) * 1.5 + 1e-12)

    def test_minus5_halves_and_the_close_books_both_segments(self):
        r, clock, e, w = self.ready_book()
        self.shock_to(w, -0.06)
        self.at(clock, e, 6)
        r.tick()
        kinds = [x["kind"] for x in self.events(r)]
        self.assertIn("day_halve", kinds)
        adj = r.ledger.adjustments()
        self.assertEqual(len(adj), 1)
        before, after = json.loads(adj[0]["weights_before"]), json.loads(adj[0]["weights_after"])
        for c in before:
            self.assertAlmostEqual(after[c], before[c] * 0.5)
        cm = CostModel()
        cost = sum(abs(before[c] - after[c]) * cm.per_side(c) for c in before)
        self.assertAlmostEqual(adj[0]["nav_after"], adj[0]["nav_mark"] * (1 - cost))
        self.assertAlmostEqual(adj[0]["nav_mark"] / r.ledger.last_run()["nav_close"] - 1, -0.06, delta=0.004)
        fills = r.ledger._db.execute("SELECT * FROM risk_fills").fetchall()
        self.assertEqual({f["coin"] for f in fills}, set(before))
        self.assertTrue(all(f["side"] == "sell" for f in fills))
        self.at(clock, e, 7)
        r.tick()
        self.assertEqual(len(r.ledger.adjustments()), 1)  # halved once per day
        self.shock = {}
        self.at(clock, e + 1, 0)
        r.tick()
        run = r.ledger.last_run()
        prev = r.ledger.runs()[-2]
        self.assertAlmostEqual(run["ret"], run["nav_close"] / prev["nav_close"] - 1, places=12)
        self.assertAlmostEqual(run["nav_open"], prev["nav_close"])
        # the last segment starts from the adjustment's NAV and prices
        self.assertGreater(run["cost"] * run["nav_open"], adj[0]["cost_usd"] - 1e-9)
        self.assertLessEqual(self.gross(json.loads(run["weights"])), self.gross(after) * 1.5)

    def test_minus8_closes_everything_and_locks_24h(self):
        r, clock, e, w = self.ready_book()
        self.shock_to(w, -0.09)
        self.at(clock, e, 6)
        r.tick()
        self.assertIn("day_flat", [x["kind"] for x in self.events(r)])
        adj = r.ledger.adjustments()
        self.assertEqual(json.loads(adj[0]["weights_after"]), {})
        lock = r.ledger.risk_get("lock")
        self.assertEqual(lock["kind"], "24h")
        self.assertEqual(lock["until"] - lock["since"], 24 * HOUR_MS)
        self.shock = {}
        self.at(clock, e + 1, 0)
        r.tick()
        self.assertEqual(json.loads(r.ledger.last_run()["weights"]), {})  # still locked at that close
        self.assertIn("lock_active", [x["kind"] for x in self.events(r)])
        self.at(clock, e + 2, 0)  # > 24h later: trading resumes
        r.tick()
        self.assertTrue(json.loads(r.ledger.last_run()["weights"]))
        s = r.summary()["risk"]
        self.assertFalse(s["locked"])
        self.assertTrue(any(x["kind"] == "day_flat" and x["label"] == "当日 −8% 全部平仓并锁 24h" for x in s["events"]))


class DrawdownTests(_Base):
    def test_minus15_halves_once_per_peak(self):
        r, clock, e, w = self.ready_book()
        nav = r.ledger.last_run()["nav_close"]
        with patch.object(r.ledger, "peak_nav", return_value=nav / 0.84):
            self.at(clock, e, 3)
            r.tick()
            self.at(clock, e, 4)
            r.tick()
        ev = self.events(r, "dd_halve")
        self.assertEqual(len(ev), 1)
        adj = r.ledger.adjustments()
        self.assertEqual(len(adj), 1)
        b, a = json.loads(adj[0]["weights_before"]), json.loads(adj[0]["weights_after"])
        self.assertAlmostEqual(self.gross(a), self.gross(b) * 0.5)
        with patch.object(r.ledger, "peak_nav", return_value=nav / 0.84):
            self.at(clock, e + 1, 0)
            r.tick()
        run = r.ledger.last_run()
        tg, rt = json.loads(run["targets"]), json.loads(run["risk_targets"])
        for c in tg:
            self.assertAlmostEqual(rt[c], 0.5 * tg[c])  # half the strategy's targets, not compounding

    def test_minus20_closes_all_and_needs_a_manual_review(self):
        r, clock, e, w = self.ready_book()
        nav = r.ledger.last_run()["nav_close"]
        with patch.object(r.ledger, "peak_nav", return_value=nav / 0.79):
            self.at(clock, e, 3)
            r.tick()
            self.assertEqual(r.ledger.risk_get("lock")["kind"], "review")
            for k in (1, 2, 3):
                self.at(clock, e + k, 0)
                r.tick()
                self.assertEqual(json.loads(r.ledger.last_run()["weights"]), {})
        self.assertIn("dd_flat", [x["kind"] for x in self.events(r)])
        self.assertTrue(r.summary()["risk"]["locked"])
        self.assertTrue(r.clear_lock("reviewed by Vinnie"))
        self.assertEqual(self.events(r)[0]["kind"], "lock_cleared")
        self.at(clock, e + 4, 0)
        r.tick()
        self.assertTrue(json.loads(r.ledger.last_run()["weights"]))


class FundingAndDataTests(_Base):
    def test_funding_above_0_1pct_halves_that_long(self):
        r, clock, e, w = self.ready_book()
        c = max(w, key=w.get)
        ts = self.panel.days[e] + DAY_MS + 8 * HOUR_MS
        self.extra_funding[(c, ts)] = 0.0015
        self.at(clock, e, 9)
        r.tick()
        ev = self.events(r, "funding")
        self.assertEqual(len(ev), 1)
        self.assertEqual(json.loads(ev[0]["detail"])["coin"], c)
        adj = r.ledger.adjustments()
        after = json.loads(adj[0]["weights_after"])
        tgt = json.loads(r.ledger.last_run()["targets"])[c]
        self.assertAlmostEqual(after[c], 0.5 * tgt)
        others = [x for x in json.loads(adj[0]["weights_before"]) if x != c]
        for x in others:
            self.assertAlmostEqual(after[x], json.loads(adj[0]["weights_before"])[x])
        self.at(clock, e, 10)
        r.tick()
        self.assertEqual(len(self.events(r, "funding")), 1)  # one settlement, one trigger

    def test_stale_marks_switch_to_reduce_only(self):
        r, clock, e, w = self.ready_book()
        c = next(iter(w))
        self.stale = {c}
        self.at(clock, e, 5)
        r.tick()
        ev = self.events(r, "data_breaker")
        self.assertEqual(ev[0]["action"], "reduce_only")
        self.assertIn(c, json.loads(ev[0]["detail"])["reason"])
        self.assertTrue(r.ledger.risk_get("data_bad"))
        self.at(clock, e + 1, 0)
        r.tick()
        run = r.ledger.last_run()
        held = json.loads(r.ledger.runs()[-2]["weights"])
        for k, x in json.loads(run["weights"]).items():
            self.assertIn(k, held)
        self.stale = set()
        self.at(clock, e + 1, 5)
        r.tick()
        self.assertEqual(self.events(r, "data_breaker")[0]["action"], "cleared")
        self.assertIsNone(r.ledger.risk_get("data_bad"))

    def test_exchange_health_failure_is_a_data_breaker(self):
        r, clock, e, w = self.ready_book()
        self.health_ok = False
        self.at(clock, e, 5)
        r.tick()
        self.assertIn("exchange down", json.loads(self.events(r, "data_breaker")[0]["detail"])["reason"])


class CapTests(_Base):
    coins = ["BTC", "ETH", "SOL"]
    vol = 0.01  # calm 3-coin tape: vol targeting asks for > 25% per coin

    def test_single_coin_and_gross_caps(self):
        w, hits = risk.cap_weights([0.6, 0.1, -0.3], RiskLimits())
        self.assertEqual(w, [0.25, 0.1, -0.25])
        self.assertEqual(hits, ["coin"])
        w, hits = risk.cap_weights([0.25] * 6, RiskLimits())
        self.assertAlmostEqual(sum(w), 1.0)
        self.assertIn("gross", hits)

    def test_three_coin_book_is_clipped_to_25pct_and_logged(self):
        clock = Clock(0)
        self.at(clock, 150, 0)
        r = self.runner(clock)
        for i in range(150, 170):
            self.at(clock, i, 0)
            r.tick()
        for x in r.ledger.runs():
            for v in json.loads(x["weights"]).values():
                self.assertLessEqual(abs(v), 0.25 + 1e-12)
        self.assertTrue(self.events(r, "cap_coin"))

    def test_no_increase_rule(self):
        self.assertEqual(risk.no_increase(0.2, 0.1), 0.1)
        self.assertEqual(risk.no_increase(0.05, 0.1), 0.05)
        self.assertEqual(risk.no_increase(0.1, 0.0), 0.0)
        self.assertEqual(risk.no_increase(-0.1, 0.1), 0.0)

    def test_f8_uses_settlement_spacing(self):
        self.assertAlmostEqual(risk.f8_of([(0, 0.0001), (4 * HOUR_MS, 0.0005)])[1], 0.001)
        self.assertAlmostEqual(risk.f8_of([(0, 0.0005)])[1], 0.0005)


if __name__ == "__main__":
    unittest.main()


class StoreMarksTests(unittest.TestCase):
    def _svc(self, last_refresh, rows):
        class Store:
            def candles(self, ex, c, tf, until=None, limit=1):
                return [r for r in rows.get(c, []) if r["ts"] <= until][-limit:]

        class Cfg:
            refresh_sec = 300

        class Svc:
            store = Store()
            cfg = Cfg()
            last_refresh_ms = last_refresh
            last_error = "okx: timeout"

            def exchange_for_read(self):
                return "okx"

        return Svc()

    def test_waits_for_the_bar_to_be_final_then_flags_a_dead_feed(self):
        bar = 1_790_000_000_000 // HOUR_MS * HOUR_MS
        rows = {"BTC": [{"ts": bar, "close": 100.0}]}
        with patch("app.marketdata.mainstream.get_service", return_value=self._svc(bar + 10 * 60_000, rows)), \
                patch("time.time", return_value=(bar + HOUR_MS + 2 * 60_000) / 1000):
            self.assertTrue(sr._store_marks(["BTC"], bar)["wait"])
        with patch("app.marketdata.mainstream.get_service", return_value=self._svc(bar + HOUR_MS + 60_000, rows)), \
                patch("time.time", return_value=(bar + HOUR_MS + 2 * 60_000) / 1000):
            m = sr._store_marks(["BTC"], bar)
            self.assertEqual(m["prices"]["BTC"], (bar, 100.0))
            self.assertTrue(m["ok"])
        with patch("app.marketdata.mainstream.get_service", return_value=self._svc(bar - 3 * HOUR_MS, rows)), \
                patch("time.time", return_value=(bar + HOUR_MS + 20 * 60_000) / 1000):
            m = sr._store_marks(["BTC"], bar)
            self.assertFalse(m["ok"])
            self.assertIn("timeout", m["reason"])
