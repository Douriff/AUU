"""A multi-day outage is not filled at current prices. An admin seal keeps history and skips the hole."""
from __future__ import annotations

import unittest
from pathlib import Path

from app.backtest.panel import DAY_MS
from app.paper import perf_report
from app.paper import strategy_runner as sr
from app.paper.shadow_h2 import H2Ledger
from tests.test_strategy_runner import Clock, _Base


class GapSealTests(_Base):
    def test_refuse_does_not_book_current_prices_and_resume_opens_a_new_epoch(self):
        anchor = 200
        clock = Clock(self.panel.days[anchor])
        r = self.runner(clock, catchup="refuse")
        self.assertEqual(r.tick(), [self.panel.days[anchor]])
        before_runs = len(r.ledger.runs())
        before_fills = len(r.ledger.fills(1000))
        anchor_nav = r.ledger.last_run()["nav_close"]
        called = {"n": 0}

        def current_prices(coins):
            called["n"] += 1
            return {"ok": True, "prices": {c: 424242.0 for c in coins}, "ts": clock.t, "exchange": "binance"}

        r.exec_px_fn = current_prices
        clock.to_after(self.panel.days[anchor + 18])
        self.assertEqual(r.tick(), [])
        self.assertEqual(called["n"], 0)
        self.assertIn("admin resume required", r.status()["waiting"])
        self.assertTrue(r.status()["resumeRequired"])
        self.assertEqual(len(r.ledger.runs()), before_runs)
        self.assertEqual(len(r.ledger.fills(1000)), before_fills)
        self.assertEqual(r.ledger.last_run()["nav_close"], anchor_nav)

        h2_path = Path(self.tmp.name) / "shadow_h2.sqlite"
        out = sr.resume_forward(now_ms=clock.t, by="admin-id", note="outage", strategy_path=r.ledger.path, h2_path=h2_path)
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["how"], "created")
        self.assertEqual(out["gap"]["days"], 18)
        self.assertTrue(out["gap"]["missing"])
        self.assertEqual(out["runs"], before_runs)
        self.assertEqual(out["fills"], before_fills)
        self.assertEqual(len(r.ledger.runs()), before_runs)
        self.assertFalse(any(abs(float(f["price"]) - 424242.0) < 1 for f in r.ledger.fills(1000)))
        h2 = H2Ledger(h2_path)
        self.assertEqual(h2.rows(), [])
        self.assertEqual(int(h2.latest_gap()["gap_to"]), int(out["gap"]["toMs"]))
        h2.close()
        again = sr.resume_forward(now_ms=clock.t, by="admin-id", note="outage", strategy_path=r.ledger.path, h2_path=h2_path)
        self.assertEqual(again["how"], "replayed")
        self.assertEqual(len(r.ledger.runs()), before_runs)
        held = r.summary()
        old_ret = (anchor_nav / 10_000.0) - 1
        self.assertTrue(held["segment"]["pending"])
        self.assertEqual(held["segment"]["days"], 0)
        self.assertEqual(held["nav"], 10_000.0)
        self.assertEqual(held["startNav"], 10_000.0)
        self.assertIsNone(held["totals"]["strategy"])
        self.assertIsNone(held["totals"]["btc"])
        self.assertIsNone(held["segment"]["lastRet"])
        self.assertAlmostEqual(held["history"]["strategy"], old_ret)
        self.assertAlmostEqual(held["history"]["endNav"], anchor_nav)
        self.assertTrue(all(p["epoch"] == 1 for p in held["curve"]))
        self.assertFalse(r.status()["stalled"])
        self.assertEqual(r.status()["phase"], "waiting_close")
        self.assertEqual(r.status()["hoursSinceRebalance"], 0)
        report = perf_report.build(r)
        self.assertEqual(report["metrics"]["days"], 0)
        self.assertTrue(report["gap"]["pending"])
        self.assertEqual(report["goNoGo"]["days"], 0)
        self.assertEqual(held["fills"], [])
        late = clock.t
        scheduled = self.panel.days[anchor + 18] + 2 * DAY_MS
        clock.t = scheduled + 3_600_000
        self.ready = False
        self.assertEqual(r.tick(), [])
        settling = r.status()
        self.assertFalse(settling["stalled"])
        self.assertEqual(settling["phase"], "waiting_data")
        self.assertGreater(settling["hoursSinceRebalance"], 0)
        self.assertLess(settling["hoursSinceRebalance"], 26)
        clock.t = scheduled + 30 * 3_600_000
        self.ready = False
        self.assertEqual(r.tick(), [])
        overdue = r.status()
        self.assertTrue(overdue["stalled"])
        self.assertEqual(overdue["phase"], "stalled")
        self.assertLess(overdue["hoursSinceRebalance"], 40)
        self.assertEqual(len(r.ledger.runs()), before_runs)
        clock.t = late
        self.ready = True

        nxt = anchor + 19
        clock.to_after(self.panel.days[nxt])
        wrote = r.tick()
        self.assertEqual(wrote, [self.panel.days[nxt]])
        self.assertEqual(called["n"], 0)
        run = dict(r.ledger.runs()[-1])
        self.assertEqual(int(run["day"]), self.panel.days[nxt])
        self.assertEqual(run["nav_open"], 10_000.0)
        self.assertEqual(int(r.ledger.runs()[0]["day"]), self.panel.days[anchor])
        self.assertEqual(r.ledger.runs()[0]["nav_close"], anchor_nav)
        for fill in r.ledger.fills(1000):
            if int(fill["day"]) == self.panel.days[nxt]:
                close = self.panel.perp_close[fill["coin"]][nxt]
                self.assertAlmostEqual(fill["price"], close)
                self.assertNotAlmostEqual(fill["price"], 424242.0)
        summary = r.summary()
        self.assertEqual(summary["historyDays"], 1)
        self.assertEqual(summary["goNoGo"]["days"], 1)
        self.assertEqual(summary["goNoGo"]["historyDays"], 1)
        self.assertEqual(summary["curve"][0]["epoch"], 1)
        self.assertEqual(summary["curve"][-1]["epoch"], 2)
        self.assertTrue(summary["curve"][-1]["breakBefore"])
        self.assertEqual(summary["curve"][-1]["tbill"], 1.0)
        self.assertFalse(summary["segment"]["pending"])
        self.assertEqual(summary["segment"]["days"], 1)
        self.assertEqual(r.status()["phase"], "ok")
        self.assertTrue(all(int(f["day"]) == self.panel.days[nxt] for f in summary["fills"]))
        fresh = perf_report.build(r)
        self.assertEqual(fresh["metrics"]["days"], 1)
        self.assertFalse(fresh["gap"]["pending"])
        self.assertEqual(fresh["startNav"], 10_000.0)
        self.assertAlmostEqual(summary["totals"]["strategy"], summary["curve"][-1]["strategy"] - 1)
        self.assertNotAlmostEqual(summary["totals"]["strategy"], old_ret)
        self.assertEqual(summary["gaps"][0]["days"], 18)
        self.assertFalse(r.status()["stalled"])
        self.assertEqual(len(r.ledger.runs()), 2)

    def test_same_day_late_path_still_books_one_day(self):
        clock = Clock(self.panel.days[200])
        r = self.runner(clock, catchup="refuse", exec_px_fn=lambda coins: {
            "ok": True, "prices": {c: self.panel.perp_close[c][200] for c in coins}, "ts": clock.t, "exchange": "binance",
        })
        self.assertEqual(len(r.tick()), 1)
        clock.t = self.panel.days[201] + DAY_MS + 3 * 60 * 60_000
        self.assertEqual(r.tick(), [self.panel.days[201]])
        self.assertEqual(r.ledger.exec_row(self.panel.days[201])["mode"], "late")
