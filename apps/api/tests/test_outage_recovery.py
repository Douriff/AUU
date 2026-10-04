"""Outage at the 08:00 rebalance / 08:15 H2 / 08:30 digest: exchanges and SMTP unreachable, then back.

Paper runner: waits (never books on old data), retries, and once the network is back books the day
as a late rebalance (延迟补做) at execution-time prices, exactly once; past the cutoff it holds.
Alerts: mail raised during the outage stays queued and goes out after it; a queued digest is
rebuilt; a late rebalance sends its own notice. H2: unchanged module, waits and records once.
"""
from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app import alerts as al
from app.backtest.panel import DAY_MS, ms_day
from app.paper import strategy_runner as sr
from app.paper.strategy_risk import HOUR_MS, RiskLimits
from tests.test_alerts import Outbox, bj_ms
from tests.test_strategy_risk import Clock, _Base as _RiskBase

MIN = 60_000


class _Outage(_RiskBase):
    """Risk-cap harness (hourly marks + funding settlements) with a switchable network."""

    def setUp(self):
        super().setUp()
        self.net = True        # exchanges reachable (market store refreshing)
        self.px_ok = True      # fresh execution prices available
        self.px_missing: set[str] = set()
        self.px_calls = 0

    def exec_px(self, coins):
        self.px_calls += 1
        if not (self.net and self.px_ok):
            return {"ok": False, "reason": "no successful market refresh (40 min): binance: timeout"}
        t = self.clock_ref()
        return {"ok": True, "prices": {c: self.path_px(c, t) for c in coins if c not in self.px_missing}, "ts": t - 2 * MIN,
                "exchange": "binance"}

    def late_runner(self, clock, **kw):
        self.clock_ref = clock
        r = self.runner(clock, exec_px_fn=self.exec_px, **kw)
        r.ready_fn = lambda d: (self.net, "" if self.net else "BTC:1d,BTC:funding")
        return r

    def book(self, s=150, e=170, **kw):
        clock = Clock(0)
        self.at(clock, s, 0)
        r = self.late_runner(clock, **kw)
        for i in range(s, e + 1):
            self.at(clock, i, 0)
            r.tick()
        self.assertEqual(r.ledger.exec_rows(), [])  # every day on time: no off-schedule rows
        return r, clock, e

    def runs_by_day(self, r):
        return {int(x["day"]): dict(x) for x in r.ledger.runs()}


class LateRebalanceTests(_Outage):
    def test_outage_waits_without_error_then_books_once_at_execution_prices(self):
        r, clock, e = self.book()
        d = self.panel.days[e + 1]
        self.net = False
        n0, f0 = len(r.ledger.runs()), len(r.ledger.fills(10_000))
        for h, m in ((0, 0), (0, 5), (0, 30), (1, 0), (2, 0)):  # 08:00 .. 10:00 Beijing, network down
            self.at(clock, e + 1, h, m)
            self.assertEqual(r.tick(), [])
        st = r.status()
        self.assertEqual(st["lastError"], "")
        self.assertIn("waiting for", st["waiting"])
        self.assertIn("延迟补做", st["waiting"])
        self.assertIn("23:00", st["waiting"])
        self.assertEqual((len(r.ledger.runs()), len(r.ledger.fills(10_000))), (n0, f0))  # nothing booked on old data
        self.net = True
        self.at(clock, e + 1, 3, 30)  # 11:30 Beijing: back
        self.assertEqual(r.tick(), [d])
        x = r.ledger.exec_row(d)
        self.assertEqual(x["mode"], "late")
        self.assertAlmostEqual(x["delay_min"], 210.0)
        self.assertIn("waiting for", x["reason"])  # what it waited for is kept as evidence
        prices = json.loads(x["prices"])
        fills = [dict(f) for f in r.ledger.fills(10_000) if f["day"] == d]
        self.assertTrue(fills, "synthetic tape should trade on this day")
        for f in fills:
            self.assertAlmostEqual(f["price"], self.path_px(f["coin"], clock.t))  # execution-time price ...
            self.assertAlmostEqual(prices[f["coin"]], f["price"])
            self.assertNotAlmostEqual(f["price"], self.panel.perp_close[f["coin"]][e + 1], places=6)  # ... not the close
        run = self.runs_by_day(r)[d]
        self.assertEqual(run["catchup"], 0)
        self.assertEqual(json.loads(run["closes"])["BTC"], self.panel.perp_close["BTC"][e + 1])  # closes stay facts
        self.assertEqual(r.status()["lastExec"]["mode"], "late")
        # idempotent: more ticks, a restart, a second process
        for m in (10, 20, 40):
            clock.t += m * MIN
            self.assertEqual(r.tick(), [])
        r2 = self.late_runner(clock)
        self.assertEqual(r2.tick(), [])
        self.assertEqual(len([x for x in r2.ledger.runs() if x["day"] == d]), 1)
        self.assertEqual(len([f for f in r2.ledger.fills(10_000) if f["day"] == d]), len(fills))
        self.assertEqual(len(r2.ledger.exec_rows()), 1)

    def test_stale_or_missing_execution_prices_wait_until_fresh(self):
        r, clock, e = self.book()
        d = self.panel.days[e + 1]
        self.at(clock, e + 1, 1)  # 09:00 Beijing, daily data is in but the refresh is old
        self.px_ok = False
        self.assertEqual(r.tick(), [])
        self.assertIn("等待最新行情", r.status()["waiting"])
        self.px_ok = True
        self.px_missing = {"ETH"}
        self.assertEqual(r.tick(), [])
        self.assertIn("ETH", r.status()["waiting"])
        self.assertIsNone(r.ledger.exec_row(d))
        self.px_missing = set()
        self.assertEqual(r.tick(), [d])
        self.assertEqual(r.status()["waiting"], "")

    def test_late_with_close_prices_equals_on_time_and_next_day_starts_from_execution_prices(self):
        # same tape twice: on time vs. 3h late with execution price == close -> identical book
        r, clock, e = self.book()
        d = self.panel.days[e + 1]
        self.at(clock, e + 1, 0)
        r.tick()
        want = self.runs_by_day(r)[d]
        self.path = Path(self.tmp.name) / "late.sqlite"
        r2, c2, _ = self.book()
        r2.exec_px_fn = lambda coins: {"ok": True, "prices": {c: self.panel.perp_close[c][e + 1] for c in coins}, "ts": c2.t,
                                      "exchange": "binance"}
        r2.funding_fn = None  # isolate the price path from the drift funding
        self.at(c2, e + 1, 3)
        r2.tick()
        got = self.runs_by_day(r2)[d]
        self.assertEqual(r2.ledger.exec_row(d)["mode"], "late")
        # no intraday adjustment -> both use the panel's funding: same marks and trades
        for k in ("nav_close", "ret", "turnover", "weights"):
            self.assertEqual(got[k], want[k], k)
        # next day: the late book's reference is the execution price
        r2.exec_px_fn = lambda coins: {"ok": True, "prices": {c: self.panel.perp_close[c][e + 1] * 1.03 for c in coins},
                                      "ts": c2.t, "exchange": "binance"}
        self.path = Path(self.tmp.name) / "late2.sqlite"
        r3, c3, _ = self.book()
        r3.funding_fn = None
        r3.exec_px_fn = r2.exec_px_fn
        self.at(c3, e + 1, 3)
        r3.tick()
        late = self.runs_by_day(r3)[d]
        w = json.loads(late["weights"])
        self.at(c3, e + 2, 0)
        r3.tick()
        nxt = self.runs_by_day(r3)[self.panel.days[e + 2]]
        want_pnl = sum(wc * (self.panel.perp_close[c][e + 2] / (self.panel.perp_close[c][e + 1] * 1.03) - 1)
                       - wc * self.panel.funding[c][e + 2] for c, wc in w.items())
        self.assertAlmostEqual(nxt["pnl"], want_pnl, places=12)
        self.assertIsNone(r3.ledger.exec_row(self.panel.days[e + 2]))  # back on schedule

    def test_funding_between_close_and_execution_is_charged_once_on_the_held_book(self):
        r, clock, e = self.book()
        d = self.panel.days[e + 1]
        self.at(clock, e + 1, 9)  # 17:00 Beijing: settlements at 00:00 and 08:00 UTC have passed
        held = json.loads(r.ledger.last_run()["weights"])
        r.tick()
        x = r.ledger.exec_row(d)
        drift = json.loads(x["drift_funding"])
        for c in held:
            self.assertAlmostEqual(drift[c], 2 * self.panel.funding[c][e + 2] / 3)
        self.at(clock, e + 2, 0)
        r.tick()
        nxt = self.runs_by_day(r)[self.panel.days[e + 2]]
        w = json.loads(self.runs_by_day(r)[d]["weights"])
        want_f = sum(wc * self.panel.funding[c][e + 2] / 3 for c, wc in w.items())  # only the 16:00 UTC settlement is left
        self.assertAlmostEqual(nxt["funding"], want_f, places=12)

    def test_past_the_cutoff_the_day_is_held_not_traded_at_an_old_price(self):
        r, clock, e = self.book()
        d = self.panel.days[e + 1]
        prev = r.ledger.last_run()
        self.net = False
        self.at(clock, e + 1, 14, 50)  # 22:50 Beijing, still down
        self.assertEqual(r.tick(), [])
        self.net = True
        self.at(clock, e + 1, 15, 30)  # 23:30 Beijing: back, but past the 23:00 cutoff
        self.assertEqual(r.tick(), [d])
        x = r.ledger.exec_row(d)
        self.assertEqual(x["mode"], "missed")
        self.assertEqual([f for f in r.ledger.fills(10_000) if f["day"] == d], [])
        run = self.runs_by_day(r)[d]
        self.assertEqual(run["turnover"], 0.0)
        self.assertEqual(run["cost"], 0.0)
        w0 = json.loads(prev["weights"])
        rets = {c: self.panel.perp_close[c][e + 1] / self.panel.perp_close[c][e] - 1 for c in w0}
        pnl = sum(w0[c] * rets[c] - w0[c] * self.panel.funding[c][e + 1] for c in w0)
        self.assertAlmostEqual(run["nav_close"], prev["nav_close"] * (1 + pnl), places=6)
        for c, wc in json.loads(run["weights"]).items():  # the held book, drifted to the close
            self.assertAlmostEqual(wc, w0[c] * (1 + rets[c]) / (1 + pnl))
        self.assertEqual(self.px_calls, 0)  # never asked for a price to trade at
        self.at(clock, e + 2, 0)  # next morning: normal on-time rebalance
        self.assertEqual(r.tick(), [self.panel.days[e + 2]])
        self.assertIsNone(r.ledger.exec_row(self.panel.days[e + 2]))

    def test_two_day_outage_holds_the_old_day_and_late_books_today(self):
        r, clock, e = self.book()
        self.net = False
        for i in (e + 1, e + 2):
            self.at(clock, i, 0, 5)
            self.assertEqual(r.tick(), [])
        self.net = True
        self.at(clock, e + 2, 2)  # 10:00 Beijing two days later
        self.assertEqual(r.tick(), [self.panel.days[e + 1], self.panel.days[e + 2]])
        self.assertEqual(r.ledger.exec_row(self.panel.days[e + 1])["mode"], "missed")
        self.assertEqual(r.ledger.exec_row(self.panel.days[e + 2])["mode"], "late")
        self.assertEqual([f for f in r.ledger.fills(10_000) if f["day"] == self.panel.days[e + 1]], [])
        self.assertEqual(r.tick(), [])

    def test_within_the_on_time_window_books_at_the_close_as_before(self):
        r, clock, e = self.book()
        self.at(clock, e + 1, 0, 14)
        r.tick()
        d = self.panel.days[e + 1]
        self.assertIsNone(r.ledger.exec_row(d))
        for f in r.ledger.fills(10_000):
            if f["day"] == d:
                self.assertEqual(f["price"], self.panel.perp_close[f["coin"]][e + 1])
        self.assertEqual(self.px_calls, 0)

    def test_cutoff_and_window_from_env(self):
        with patch.dict("os.environ", {"AUU_STRATEGY_LATE_CUTOFF_BJ": "20:30", "AUU_STRATEGY_ON_TIME_MIN": "10"}):
            self.assertEqual(sr.on_time_ms(), 10 * MIN)
            self.assertEqual(sr.late_cutoff_ms(), (12 * 60 + 30) * MIN)
        with patch.dict("os.environ", {"AUU_STRATEGY_LATE_CUTOFF_BJ": "08:05"}):  # inside the on-time window: default
            self.assertEqual(sr.late_cutoff_ms(), 15 * HOUR_MS)
        with patch.dict("os.environ", {"AUU_STRATEGY_LATE_CUTOFF_BJ": "garbage"}):
            self.assertEqual(sr.late_cutoff_ms(), 15 * HOUR_MS)
        with patch.dict("os.environ", {"AUU_STRATEGY_LATE_REBALANCE": "off"}):
            self.assertFalse(sr.late_rebalance_enabled())
        self.assertTrue(sr.late_rebalance_enabled())

    def test_late_book_reports_in_summary_and_status(self):
        r, clock, e = self.book()
        self.at(clock, e + 1, 2)
        r.tick()
        s = r.summary()
        self.assertEqual(s["curve"][-1]["exec"], "late")
        self.assertEqual(s["curve"][-2]["exec"], "close")
        self.assertEqual(s["execLog"][0]["label"], "延迟补做（按执行时价格）")
        pol = s["status"]["outage"]
        self.assertTrue(pol["lateMode"])
        self.assertEqual(pol["cutoffBJ"], "23:00")
        self.assertIn("不会用旧价下单", pol["rule"])


class MonitorDuringOutageTests(_Outage):
    def test_no_intraday_trade_while_the_exchange_is_unreachable(self):
        r, clock, e = self.book()
        w = json.loads(r.ledger.last_run()["weights"])
        g = sum(abs(x) for x in w.values())
        self.shock = {c: 1 - 0.10 / g for c in w}  # stale last prints say -10%: would flatten + lock 24h
        self.health_ok = False
        self.at(clock, e, 3, 5)
        r.tick()
        self.assertEqual(r.ledger.adjustments(), [])
        kinds = {x["kind"] for x in r.ledger.risk_events(100)}
        self.assertEqual(kinds, {"data_breaker"})
        self.assertIsNone(r.ledger.risk_get("lock"))
        self.health_ok = True  # back: the caps act on the first fresh mark
        self.at(clock, e, 4, 5)
        r.tick()
        self.assertTrue(r.ledger.adjustments())
        self.assertIn("day_flat", {x["kind"] for x in r.ledger.risk_events(100)})

    def test_late_rebalance_clears_a_leftover_data_breaker_and_marks_start_after_execution(self):
        r, clock, e = self.book()
        self.health_ok = False
        self.at(clock, e, 23, 5)  # 07:05 Beijing: outage starts before the close
        r.tick()
        self.assertTrue(r.ledger.risk_get("data_bad"))
        self.health_ok = True
        self.at(clock, e + 1, 2, 30)  # late rebalance at 10:30 Beijing
        d = self.panel.days[e + 1]
        self.assertEqual(r.tick(), [d])
        self.assertIsNone(r.ledger.risk_get("data_bad"))
        ev = [dict(x) for x in r.ledger.risk_events(100) if x["kind"] == "data_breaker"]
        self.assertEqual(ev[0]["action"], "cleared")
        self.assertIn("late rebalance", ev[0]["detail"])
        self.assertNotIn("reduce_only", [x["action"] for x in ev if x["day"] == d])
        # the 10:00 bar closed before the execution: not marked against execution prices
        last_mark = r.ledger.risk_get("last_mark")
        self.assertTrue(last_mark is None or last_mark["ts"] < d + DAY_MS)
        self.at(clock, e + 1, 3, 5)  # 11:05 Beijing: first bar after the execution is marked
        r.tick()
        self.assertEqual(r.ledger.risk_get("last_mark")["ts"], d + DAY_MS + 3 * HOUR_MS)


class AlertQueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = Clock(bj_ms("2026-10-05 08:30"))
        self.box = Outbox()
        self.c = al.AlertCenter(Path(self.tmp.name) / "alerts.sqlite", send=self.box, configured=lambda: True, now_ms=self.clock)

    def tearDown(self):
        self.c.close()
        self.tmp.cleanup()

    def test_mail_is_queued_through_a_long_outage_and_sent_once_after(self):
        self.box.fail = 10_000
        self.assertEqual(self.c.raise_alert("k1", "data", "AUUTRADE 告警：数据异常", "b1"), "failed")
        self.assertEqual(self.c.raise_alert("k2", "data", "AUUTRADE 告警：另一条", "b2"), "failed")
        for _ in range(int(3.5 * 60 / 2)):  # 3.5h outage, monitor every 2 min
            self.clock.t += 2 * MIN
            self.c.retry_failed()
        rows = {r["key"]: dict(r) for r in self.c.rows(10)}
        self.assertEqual(rows["k1"]["status"], "failed")
        self.assertGreater(rows["k1"]["attempts"], 5)  # kept retrying past the old 5-attempt limit
        self.assertLessEqual(int(rows["k1"]["next_try"]) - self.clock.t, 10 * MIN)  # at most every 10 min
        self.assertEqual(self.c.status()["queued"], 2)
        self.assertEqual(self.c.raise_alert("k1", "data", "again", "b"), "duplicate")  # no second copy queued
        self.box.fail = 0
        self.clock.t += 10 * MIN
        self.assertEqual(self.c.retry_failed(), 2)
        self.assertEqual([s for _, s, _ in self.box.sent],
                         ["AUUTRADE 告警：数据异常（排队重发，原定 08:30）", "AUUTRADE 告警：另一条（排队重发，原定 08:30）"])
        self.assertIn("排队重发", self.box.sent[0][2])
        self.assertTrue(self.box.sent[0][2].endswith("b1"))
        self.assertEqual(self.c.status()["queued"], 0)
        self.clock.t += 30 * MIN
        self.assertEqual(self.c.retry_failed(), 0)
        self.assertEqual(len(self.box.sent), 2)

    def test_a_retry_pass_stops_at_the_first_failure(self):
        self.box.fail = 3
        for k in ("a", "b", "c"):
            self.c.raise_alert(k, "data", k, k)
        calls = []
        real = self.c.send
        self.c.send = lambda *a: (calls.append(a), real(*a))[1]
        self.box.fail = 1_000
        self.clock.t += 5 * MIN
        self.c.retry_failed()
        self.assertEqual(len(calls), 1)

    def test_a_mail_older_than_the_queue_window_is_not_resent(self):
        self.box.fail = 10_000
        self.c.raise_alert("old", "data", "s", "b")
        self.clock.t += al.QUEUE_MS + MIN
        self.box.fail = 0
        self.assertEqual(self.c.retry_failed(), 0)
        self.assertEqual(self.box.sent, [])


class OutageMailTests(_Outage):
    def setUp(self):
        super().setUp()
        self.box = Outbox()
        self.fresh = {"enabled": True, "exchange": "binance", "stale": False, "staleSeries": [], "lastRefreshMs": 0}
        self.r, self.clock, self.e = self.book()
        self.center = al.AlertCenter(Path(self.tmp.name) / "alerts.sqlite", send=self.box, configured=lambda: True, now_ms=self.clock)
        self.mon = al.AlertMonitor(self.center, runner_fn=lambda: self.r, freshness_fn=lambda: self.fresh)

    def tearDown(self):
        self.center.close()
        super().tearDown()

    def test_rebalance_digest_and_notice_across_an_outage(self):
        e, r, mon = self.e, self.r, self.mon
        d = self.panel.days[e + 1]
        self.at(self.clock, e, 23, 50)  # 07:50 Beijing: arm before the slot
        mon.check()
        self.net = False
        self.box.fail = 10_000  # SMTP unreachable too
        for h, m in ((0, 0), (0, 30), (1, 0), (1, 30), (2, 0), (2, 2)):  # 08:00 .. 10:02 Beijing
            self.at(self.clock, e + 1, h, m)
            r.tick()
            mon.check()
        digest = [x for x in self.center.rows(50) if x["kind"] == "digest"]
        self.assertEqual(len(digest), 1)  # built at 10:00 (stopped waiting for the rebalance), queued
        self.assertEqual(digest[0]["status"], "failed")
        self.assertIn("今日调仓尚未完成", digest[0]["body"])
        self.assertEqual(self.box.sent, [])
        # 11:30 Beijing: network and SMTP back
        self.net, self.box.fail = True, 0
        self.at(self.clock, e + 1, 3, 30)
        self.assertEqual(r.tick(), [d])
        mon.check()
        mon.check()
        subjects = [s for _, s, _ in self.box.sent]
        notice = [b for _, s, b in self.box.sent if "延迟补做" in s]
        self.assertEqual(len(notice), 1, subjects)
        self.assertIn("实际执行：北京时间", notice[0])
        self.assertIn("按执行时的最新行情记账", notice[0])
        digests = [(s, b) for _, s, b in self.box.sent if "每日摘要" in s]
        self.assertEqual(len(digests), 1, subjects)
        self.assertIn("排队重发", digests[0][0])
        self.assertIn("延迟补做", digests[0][1])  # rebuilt: shows the late rebalance, not the 10:00 state
        self.assertNotIn("今日调仓尚未完成", digests[0][1])
        self.assertIn(ms_day(d), digests[0][1])
        for _ in range(5):
            self.clock.t += 10 * MIN
            r.tick()
            mon.check()
        self.assertEqual(len([s for _, s, _ in self.box.sent if "延迟补做" in s]), 1)
        self.assertEqual(len([s for _, s, _ in self.box.sent if "每日摘要" in s]), 1)

    def test_missed_day_sends_a_hold_notice(self):
        e, r = self.e, self.r
        self.at(self.clock, e, 23, 50)
        self.mon.check()
        self.net = False
        self.at(self.clock, e + 1, 15, 30)
        r.tick()
        self.net = True
        self.at(self.clock, e + 1, 15, 40)  # 23:40 Beijing
        r.tick()
        self.mon.check()
        notice = [(s, b) for _, s, b in self.box.sent if "已错过" in s]
        self.assertEqual(len(notice), 1)
        self.assertIn("保持原仓位", notice[0][1])


class ExecPriceSourceTests(unittest.TestCase):
    """Production price source for a late rebalance: the market store, only when freshly refreshed."""

    def setUp(self):
        from app.marketdata.mainstream.store import MarketStore

        self.tmp = tempfile.TemporaryDirectory()
        self.store = MarketStore(Path(self.tmp.name) / "m.sqlite")
        self.now = int(time.time() * 1000)
        self.svc = SimpleNamespace(cfg=SimpleNamespace(refresh_sec=300), last_refresh_ms=self.now - 2 * MIN, last_error=None,
                                   store=self.store, exchange_for_read=lambda: "binance")
        bar = self.now // HOUR_MS * HOUR_MS
        for c, px in (("BTC", 60_000.0), ("ETH", 3_000.0)):
            self.store.upsert_candles("binance", c, "1h", [[bar - HOUR_MS, px, px, px, px * 0.99, 1], [bar, px, px, px, px, 1]])
            self.store.log_fetch("binance", c, "1h", attempt_ms=self.now - 2 * MIN, ok=True, error=None, rows=1)
        self.store.upsert_candles("binance", "SOL", "1h", [[bar - 5 * HOUR_MS, 1, 1, 1, 150.0, 1]])
        self.store.log_fetch("binance", "SOL", "1h", attempt_ms=self.now - 5 * HOUR_MS, ok=True, error=None, rows=1)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def call(self, coins):
        with patch("app.marketdata.mainstream.get_service", lambda: self.svc):
            return sr._store_exec_prices(coins)

    def test_fresh_store_gives_the_forming_bar_close_and_skips_old_coins(self):
        out = self.call(["BTC", "ETH", "SOL"])
        self.assertTrue(out["ok"])
        self.assertEqual(out["prices"], {"BTC": 60_000.0, "ETH": 3_000.0})  # SOL's price is hours old: left out -> runner waits
        self.assertEqual(out["exchange"], "binance")

    def test_no_recent_refresh_means_no_price(self):
        self.svc.last_refresh_ms = self.now - 40 * MIN
        self.svc.last_error = "binance: RequestTimeout"
        out = self.call(["BTC"])
        self.assertFalse(out["ok"])
        self.assertIn("no successful market refresh", out["reason"])
        self.svc.last_refresh_ms = None
        self.assertFalse(self.call(["BTC"])["ok"])


class H2OutageTests(unittest.TestCase):
    """H2 already waits for final data and catches up once; the module is unchanged (frozen hash)."""

    def test_h2_waits_through_the_outage_and_records_each_day_once(self):
        from tests import test_shadow_h2 as t
        from app.paper import shadow_h2 as h2

        case = t._Base("run")
        case.setUp()
        try:
            self.assertEqual(h2.params_hash(h2.PARAMS), h2.FROZEN_SHA256)
            self.assertTrue(h2.FROZEN_SHA256.startswith("4795b3a1"))
            down = {"v": True}
            real_perp = case.perp

            def perp(ex, c, lo, hi):
                if down["v"]:
                    return []  # _perp_klines returns [] on FetchError
                return real_perp(ex, c, lo, hi)

            sh = h2.ShadowH2(case.led, panel_fn=case.panel_until, perp_fn=perp, exchange_fn=lambda: "binance", now_ms=case.clock,
                             ready_fn=lambda d: (not down["v"], "BTC:1d" if down["v"] else ""))
            for m in (15, 20, 30, 60, 120):  # 08:15 .. 10:00 Beijing, network down
                case.clock.t = t.INC + DAY_MS + m * MIN
                self.assertEqual(sh.tick(), [])
                self.assertEqual(sh.last_error, "")
            self.assertEqual(case.led.rows(), [])
            down["v"] = False
            case.clock.t = t.INC + DAY_MS + 210 * MIN
            self.assertEqual(sh.tick(), [t.INC])
            self.assertEqual(sh.tick(), [])
            case.clock.t += 30 * MIN
            self.assertEqual(sh.tick(), [])
            self.assertEqual(len(case.led.rows()), 1)
        finally:
            case.tearDown()


if __name__ == "__main__":
    unittest.main()
