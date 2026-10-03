"""M3 daily paper runner: same decide()/costs as the backtest, idempotent, recovers missed days."""
from __future__ import annotations

import json
import math
import os
import random
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from app.backtest import engine
from app.backtest.costs import CostModel
from app.backtest.panel import DAY_MS, Panel, day_ms
from app.paper import strategy_runner as sr
from app.strategies.trend_tsmom import TrendTSMOM

D0 = day_ms("2025-01-01")
N = 300
COINS = ["BTC", "ETH", "SOL"]


def make_panel(n=N, seed=3, coins=None) -> Panel:
    coins = list(coins or COINS)
    rnd = random.Random(seed)
    days = [D0 + i * DAY_MS for i in range(n)]
    sc, fu = {}, {}
    for k, c in enumerate(coins):
        px, out = 100.0 * (k + 1), []
        drift = (0.002, -0.001, 0.0015, 0.001, -0.0005)[k % 5]
        for i in range(n):
            # regime switch halfway so the long-only signal both enters and exits
            px *= math.exp((drift if i < n // 2 else -drift) + rnd.gauss(0, 0.03))
            out.append(px)
        sc[c] = out
        fu[c] = [0.0001 * (1 + (i % 3)) for i in range(n)]
    return Panel(days, coins, sc, {c: list(v) for c, v in sc.items()}, fu, source="synthetic")


class Clock:
    def __init__(self, day: int):
        self.t = day + DAY_MS + 10 * 60_000  # 00:10 UTC the morning after `day` closed

    def __call__(self):
        return self.t

    def to_after(self, day: int, minutes: int = 10):
        self.t = day + DAY_MS + minutes * 60_000


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "s.sqlite"
        self.panel = make_panel()
        self.ready = True
        self.ledgers = []

    def tearDown(self):
        for l in self.ledgers:
            l.close()
        self.tmp.cleanup()

    def panel_fn(self, day):
        return self.panel.truncate(self.panel.days.index(day) + 1)

    def runner(self, clock, **kw):
        led = sr.StrategyLedger(self.path, now_ms=clock)
        self.ledgers.append(led)
        return sr.StrategyRunner(led, panel_fn=self.panel_fn, ready_fn=lambda d: (self.ready, "" if self.ready else "BTC:1d"),
                                 start_nav=10_000.0, now_ms=clock, **kw)

    def run_days(self, r, clock, first, last):
        for i in range(first, last + 1):
            clock.to_after(self.panel.days[i])
            r.tick()


class SameAsBacktestTests(_Base):
    def test_daily_runner_reproduces_backtest_engine(self):
        s, e = 130, 220
        clock = Clock(self.panel.days[s])
        r = self.runner(clock)
        self.run_days(r, clock, s, e)
        runs = r.ledger.runs()
        self.assertEqual(len(runs), e - s + 1)
        strat = TrendTSMOM()
        from app.backtest.panel import ms_day

        bt = engine.run(self.panel, strat.targets(self.panel), start=ms_day(self.panel.days[s]), end=ms_day(self.panel.days[e]),
                        cost=CostModel(), band=0.2, eligible=strat.eligibility(self.panel))
        self.assertGreater(sum(bt.turnover), 0.5)  # the tape actually trades
        for got, want in zip([x["ret"] for x in runs], bt.returns):
            self.assertAlmostEqual(got, want, places=12)
        nav = 10_000.0
        for x in bt.returns:
            nav *= 1 + x
        self.assertAlmostEqual(runs[-1]["nav_close"], nav, places=6)
        self.assertTrue(any(x["funding"] != 0 for x in runs))  # funding charged on held weights

    def test_fills_use_the_backtest_cost_model(self):
        clock = Clock(self.panel.days[150])
        r = self.runner(clock)
        self.run_days(r, clock, 150, 170)
        cm = CostModel()
        by_day = {}
        for f in r.ledger.fills(1000):
            self.assertAlmostEqual(f["fee"], f["notional"] * cm.taker)
            self.assertAlmostEqual(f["slippage"], f["notional"] * cm.slippage.get(f["coin"], cm.slippage_default))
            want_px = f["price"] * (1 + cm.slippage[f["coin"]] if f["side"] == "buy" else 1 - cm.slippage[f["coin"]])
            self.assertAlmostEqual(f["fill_price"], want_px)
            by_day[f["day"]] = by_day.get(f["day"], 0.0) + f["fee"] + f["slippage"]
        self.assertTrue(by_day)
        for x in r.ledger.runs():
            if x["day"] in by_day:
                nav_trade = x["nav_open"] * (1 + x["pnl"])
                self.assertAlmostEqual(by_day[x["day"]] / nav_trade, x["cost"], places=12)


class IdempotencyTests(_Base):
    def test_tick_twice_and_restart_never_trade_twice(self):
        clock = Clock(self.panel.days[160])
        r = self.runner(clock)
        self.assertEqual(r.tick(), [self.panel.days[160]])
        self.assertEqual(r.tick(), [])
        n_fills = len(r.ledger.fills(1000))
        clock.t += 3 * 3_600_000  # later the same day, after a "restart"
        r2 = self.runner(clock)
        self.assertEqual(r2.tick(), [])
        self.assertEqual(len(r2.ledger.runs()), 1)
        self.assertEqual(len(r2.ledger.fills(1000)), n_fills)

    def test_two_processes_racing_write_each_day_once(self):
        clock = Clock(self.panel.days[160])
        a, b = self.runner(clock), self.runner(clock)
        out = []
        ts = [threading.Thread(target=lambda r=r: out.append(r.tick())) for r in (a, b) for _ in range(3)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(sum(len(x) for x in out), 1)
        self.assertEqual(len(a.ledger.runs()), 1)

    def test_crash_before_commit_retries_with_identical_result(self):
        clock = Clock(self.panel.days[160])
        ref = self.runner(clock)
        self.run_days(ref, clock, 160, 165)
        want = ref.ledger.runs()[-1]["nav_close"]
        self.path = Path(self.tmp.name) / "crash.sqlite"
        clock2 = Clock(self.panel.days[160])
        r = self.runner(clock2)
        real = r.ledger.commit_day
        boom = {"n": 1}

        def flaky(*a, **kw):
            if boom["n"]:
                boom["n"] -= 1
                raise OSError("disk full (simulated crash before commit)")
            return real(*a, **kw)

        r.ledger.commit_day = flaky
        self.assertEqual(r.tick(), [])
        self.assertIn("simulated", r.status()["lastError"])
        self.assertEqual(r.ledger.runs(), [])
        self.run_days(r, clock2, 160, 165)
        self.assertAlmostEqual(r.ledger.runs()[-1]["nav_close"], want, places=9)


class RecoveryTests(_Base):
    def test_missed_days_are_replayed_in_order_and_match_a_continuous_run(self):
        clock = Clock(self.panel.days[140])
        ref = self.runner(clock)
        self.run_days(ref, clock, 140, 160)
        want = [(x["day"], x["nav_close"]) for x in ref.ledger.runs()]
        self.path = Path(self.tmp.name) / "gap.sqlite"
        c2 = Clock(self.panel.days[140])
        r = self.runner(c2)
        self.run_days(r, c2, 140, 150)
        c2.to_after(self.panel.days[160])  # API was down for 10 days
        written = r.tick()
        self.assertEqual(len(written), 10)
        got = [(x["day"], x["nav_close"]) for x in r.ledger.runs()]
        self.assertEqual([d for d, _ in got], [d for d, _ in want])
        for (_, g), (_, w) in zip(got, want):
            self.assertAlmostEqual(g, w, places=9)
        flags = [x["catchup"] for x in r.ledger.runs()]
        self.assertEqual(flags[-1], 0)
        self.assertEqual(sum(flags), 9)

    def test_gap_too_large_is_not_replayed_blindly(self):
        clock = Clock(self.panel.days[140])
        r = self.runner(clock, max_catchup_days=5)
        r.tick()
        clock.to_after(self.panel.days[150])
        self.assertEqual(r.tick(), [])
        self.assertIn("manual review", r.status()["waiting"])

    def test_waits_for_final_daily_bar(self):
        clock = Clock(self.panel.days[140])
        r = self.runner(clock)
        self.ready = False
        self.assertEqual(r.tick(), [])
        self.assertIn("waiting for", r.status()["waiting"])
        self.ready = True
        self.assertEqual(len(r.tick()), 1)


class StallAndGoTests(_Base):
    def test_stall_after_26h_without_rebalance(self):
        clock = Clock(self.panel.days[140])
        r = self.runner(clock)
        self.assertFalse(r.status()["stalled"])  # fresh ledger
        r.tick()
        clock.t = self.panel.days[140] + DAY_MS + 25 * 3_600_000
        self.assertFalse(r.status()["stalled"])
        clock.t = self.panel.days[140] + DAY_MS + 27 * 3_600_000
        st = r.status()
        self.assertTrue(st["stalled"])
        self.assertIn("26", st["reason"])

    def test_stall_before_first_run_counts_from_ledger_creation(self):
        clock = Clock(self.panel.days[140])
        r = self.runner(clock)
        self.ready = False
        clock.t += 27 * 3_600_000
        r2 = self.runner(clock)  # restart does not reset the clock
        self.assertTrue(r2.status()["stalled"])

    def test_go_no_go_waits_for_250_daily_returns(self):
        clock = Clock(self.panel.days[40])
        r = self.runner(clock, max_catchup_days=400)
        r.tick()
        clock.to_after(self.panel.days[40 + 248])
        r.tick()
        g = r.go_no_go()
        self.assertEqual((g["days"], g["verdict"]), (249, "pending"))
        self.assertIn("数据积累中，未证明优势", g["message"])
        clock.to_after(self.panel.days[40 + 249])
        r.tick()
        g = r.go_no_go()
        self.assertEqual(g["days"], 250)
        self.assertIn(g["verdict"], ("go", "no-go"))
        self.assertEqual(g["standard"], "daily_returns")
        self.assertIn("ci_lo", g["stats"])

    def test_summary_has_curve_benchmarks_and_positions(self):
        clock = Clock(self.panel.days[150])
        r = self.runner(clock)
        self.run_days(r, clock, 150, 160)
        s = r.summary()
        self.assertEqual(len(s["curve"]), 11)
        self.assertEqual(s["curve"][0]["btc"], 1.0)
        self.assertEqual(s["curve"][0]["tbill"], 1.0)
        self.assertAlmostEqual(s["curve"][-1]["btc"], self.panel.spot_close["BTC"][160] / self.panel.spot_close["BTC"][150])
        self.assertEqual(s["live"]["enabled"], False)
        self.assertEqual(s["goNoGo"]["verdict"], "pending")
        for p in s["positions"]:
            self.assertAlmostEqual(p["notional"], p["weight"] * s["nav"])

    def test_runner_has_no_exchange_order_path(self):
        src = Path(sr.__file__).read_text(encoding="utf-8")
        for bad in ("ccxt", "create_order", "make_exchange", "apiKey", "secret", "app.live"):
            self.assertNotIn(bad, src)


class StoreReadinessTests(unittest.TestCase):
    def test_daily_bar_is_final_only_once_the_next_bar_exists(self):
        D = day_ms("2026-10-02")

        class Store:
            def __init__(self, last_1d, last_f):
                self.l1, self.lf = last_1d, last_f

            def candle_bounds(self, ex, c, tf):
                return (0, self.l1, 1)

            def funding_bounds(self, ex, c):
                return (0, self.lf, 1)

        class Svc:
            def __init__(self, store):
                self.store = store
                self.cfg = type("C", (), {"symbols": ["BTC"], "strategy_symbols": ["BTC", "ETH"]})()

            def exchange_for_read(self):
                return "binance"

        cases = [((D, D + 16 * 3_600_000), False), ((D + DAY_MS, D + 8 * 3_600_000), False), ((D + DAY_MS, D + 16 * 3_600_000), True)]
        for (l1, lf), want in cases:
            with patch("app.marketdata.mainstream.get_service", return_value=Svc(Store(l1, lf))):
                self.assertEqual(sr._store_ready(D)[0], want, (l1, lf))


class UniverseTests(_Base):
    def setUp(self):
        super().setUp()
        self.panel = make_panel(coins=["BTC", "ETH", "SOL", "XRP", "ADA"])
        self.switch_at = 160

    def panel_fn(self, day):
        p = self.panel.truncate(self.panel.days.index(day) + 1)
        return p.subset(["BTC", "ETH", "SOL"]) if day < self.panel.days[self.switch_at] else p

    def test_switch_is_recorded_and_history_is_not_rebuilt(self):
        clock = Clock(self.panel.days[150])
        r = self.runner(clock)
        self.run_days(r, clock, 150, 159)
        before = [dict(x) for x in r.ledger.runs()]
        self.run_days(r, clock, 160, 163)
        runs = r.ledger.runs()
        self.assertEqual([dict(x) for x in runs[:10]], before)  # earlier days untouched
        ch = r.ledger.universe_changes()
        self.assertEqual(len(ch), 1)
        self.assertEqual(ch[0]["day"], self.panel.days[160])
        self.assertEqual(sorted(json.loads(ch[0]["prev"])), ["BTC", "ETH", "SOL"])
        self.assertEqual(len(json.loads(ch[0]["coins"])), 5)
        self.assertEqual(len(json.loads(runs[10]["universe"])), 5)
        s = r.summary()["universe"]
        self.assertEqual(s["changes"][0]["day"], "2025-06-10")
        self.assertEqual(len(s["current"]), 5)
        self.assertIn("幸存者偏差", s["survivorship"])

    def test_legacy_ledger_without_universe_column(self):
        import sqlite3

        db = sqlite3.connect(self.path)
        db.executescript(sr._SCHEMA.replace(",\n  universe TEXT", ""))
        db.execute("INSERT INTO runs(day,strategy,ran_at,catchup,nav_open,pnl,funding,cost,ret,nav_close,gross_before,gross_after,turnover,weights,targets,closes)"
                   " VALUES (?, 't', 0, 0, 10000, 0, 0, 0, 0, 10000, 0, 0, 0, '{}', '{\"BTC\":0,\"ETH\":0,\"SOL\":0}', '{}')", (self.panel.days[170],))
        db.commit()
        db.close()
        clock = Clock(self.panel.days[171])
        r = self.runner(clock)
        self.assertEqual(r.tick(), [self.panel.days[171]])
        ch = r.ledger.universe_changes()
        self.assertEqual(len(ch), 1)
        self.assertEqual(sorted(json.loads(ch[0]["prev"])), ["BTC", "ETH", "SOL"])
        self.assertIsNone(r.ledger.runs()[0]["universe"])  # old row left as it was


class StoreUniverseTests(unittest.TestCase):
    def test_unavailable_coin_is_listed_and_not_waited_for(self):
        D = day_ms("2026-10-02")

        class Store:
            def candle_bounds(self, ex, c, tf):
                return (None, None, 0) if c == "FIL" else (D - 400 * DAY_MS, D + DAY_MS, 401)

            def funding_bounds(self, ex, c):
                return (D - 700 * DAY_MS, D + 16 * 3_600_000, 2100)

        svc = type("S", (), {})()
        svc.store = Store()
        svc.cfg = type("C", (), {"symbols": ["BTC"], "strategy_symbols": ["BTC", "ETH", "FIL"]})()
        svc.exchange_for_read = lambda: "okx"
        with patch("app.marketdata.mainstream.get_service", return_value=svc):
            u = sr._store_universe()
            self.assertEqual(list(u["coverage"]), ["BTC", "ETH"])
            self.assertIn("FIL", u["unavailable"])
            self.assertEqual(sr._store_ready(D), (True, ""))

    def test_default_universe_is_the_research_list(self):
        from app.backtest.panel import UNIVERSE_19
        from app.marketdata.mainstream.config import STRATEGY_UNIVERSE, load_config

        self.assertEqual(STRATEGY_UNIVERSE, UNIVERSE_19)
        with patch.dict(os.environ, {"AUU_STRATEGY_UNIVERSE": ""}):
            cfg = load_config()
        self.assertEqual(cfg.strategy_symbols, UNIVERSE_19)
        self.assertEqual(cfg.all_symbols()[:3], cfg.symbols)
        self.assertEqual(len(cfg.all_symbols()), len(set(cfg.symbols) | set(UNIVERSE_19)))


_ENV = ("AUU_AUTH", "AUU_ALLOW_SIGNUP", "AUU_INVITE_CODE", "AUU_ADMIN_USER", "AUU_USER_STORE", "AUU_USER_JOURNAL_DIR", "AUU_AUTH_RATE_MAX", "AUU_LEGACY_PUMP")
PW = "paperPass123"


class StrategyApiTests(_Base):
    def setUp(self):
        super().setUp()
        from app.auth.accounts import reset_accounts as reset_users

        self._prev = {k: os.environ.get(k) for k in _ENV}
        root = Path(self.tmp.name)
        os.environ.update({"AUU_AUTH": "on", "AUU_ALLOW_SIGNUP": "on", "AUU_USER_STORE": str(root / "users.json"),
                           "AUU_USER_JOURNAL_DIR": str(root / "journals"), "AUU_AUTH_RATE_MAX": "1000", "AUU_LEGACY_PUMP": "off"})
        os.environ.pop("AUU_INVITE_CODE", None)
        os.environ.pop("AUU_ADMIN_USER", None)
        reset_users()
        self.clock = Clock(self.panel.days[150])
        self.r = self.runner(self.clock)
        self.run_days(self.r, self.clock, 150, 155)
        sr.reset_runner(self.r)
        self._sock = patch.object(socket.socket, "connect", side_effect=AssertionError("network access in test"))
        self._sock.start()
        from fastapi.testclient import TestClient
        from app.main import create_app

        self.TestClient = TestClient
        self.app = create_app(legacy=False)

    def tearDown(self):
        from app.auth.accounts import reset_accounts as reset_users

        self._sock.stop()
        sr.reset_runner(None)
        for k, v in self._prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        reset_users()
        super().tearDown()

    def test_unauthenticated_strategy_view_returns_401(self):
        anon = self.TestClient(self.app)
        r = anon.get("/api/v1/mainstream/strategy")
        self.assertEqual(r.status_code, 401, r.text)
        self.assertEqual(r.json()["error"]["code"], "AUTH_REQUIRED")
        bad = self.TestClient(self.app, cookies={"auu_session": "forged.9999999999.sig"})
        self.assertEqual(bad.get("/api/v1/mainstream/strategy").status_code, 401)

    def test_report_route_requires_login_and_returns_report(self):
        anon = self.TestClient(self.app)
        self.assertEqual(anon.get("/api/v1/mainstream/strategy/report").status_code, 401)
        from starlette.requests import Request
        from app.routes import mainstream_strategy

        req = Request({"type": "http", "method": "GET", "path": "/", "headers": [], "query_string": b""})
        self.assertEqual(mainstream_strategy.strategy_report(req).status_code, 401)
        c = self.TestClient(self.app)
        self.assertEqual(c.post("/api/v1/auth/register", json={"name": "vin2", "password": PW, "password_confirm": PW}).status_code, 200)
        d = c.get("/api/v1/mainstream/strategy/report").json()["data"]
        self.assertEqual(d["metrics"]["days"], 6)
        self.assertEqual(len(d["drawdown"]), 6)
        self.assertIn("coins", d["attribution"])

    def test_route_rechecks_session_even_without_gate(self):
        from starlette.requests import Request
        from app.routes import mainstream_strategy

        req = Request({"type": "http", "method": "GET", "path": "/", "headers": [], "query_string": b""})
        self.assertEqual(mainstream_strategy.strategy_summary(req).status_code, 401)

    def test_logged_in_console_view_and_health(self):
        c = self.TestClient(self.app)
        self.assertEqual(c.post("/api/v1/auth/register", json={"name": "vin", "password": PW, "password_confirm": PW}).status_code, 200)
        d = c.get("/api/v1/mainstream/strategy").json()["data"]
        self.assertEqual(len(d["curve"]), 6)
        self.assertEqual(d["goNoGo"]["verdict"], "pending")
        self.assertFalse(d["live"]["enabled"])
        with patch("app.marketdata.mainstream.get_service") as gs:
            gs.return_value.cfg.enabled = True
            gs.return_value.freshness.return_value = {"enabled": True, "stale": False}
            h = c.get("/api/v1/health").json()["data"]
        self.assertEqual(h["runningStrategies"], ["trend_tsmom_v1"])
        self.assertEqual(h["strategyRunner"]["lastDay"], "2025-06-05")
        self.assertFalse(h["autopaperStall"]["stalled"])
        self.assertFalse(h["liveEnabled"])
        self.clock.t += 2 * DAY_MS  # no rebalance since: health and the guard's stall field fire
        with patch("app.marketdata.mainstream.get_service") as gs:
            gs.return_value.cfg.enabled = True
            gs.return_value.freshness.return_value = {"enabled": True, "stale": False}
            h = c.get("/api/v1/health").json()["data"]
        self.assertTrue(h["strategyRunner"]["stalled"])
        self.assertTrue(h["autopaperStall"]["stalled"])
        from app.paper.events import console_stats

        cs = console_stats()
        self.assertEqual(cs["verdict"], "pending")
        self.assertIn("数据积累中，未证明优势", cs["nogo_reason"])


if __name__ == "__main__":
    unittest.main()
