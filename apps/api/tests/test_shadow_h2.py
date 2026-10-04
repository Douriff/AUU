"""H2 forward shadow: frozen-parameter hash check, three-leg booking, schedule, isolation from the paper ledgers."""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import random
import socket
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.backtest import stats
from app.backtest.costs import CostModel
from app.backtest.panel import DAY_MS, Panel, day_ms, ms_day
from app.paper import shadow_h2 as h2
from app.strategies import trend_b058

INC = day_ms("2026-10-04")
COINS = ["BTC", "ETH", "SOL", "XRP"]


def synth_panel(n_days: int = 420, end: int = INC + 40 * DAY_MS, seed: int = 3, drift: float = 0.002) -> Panel:
    rng = random.Random(seed)
    days = [end - (n_days - 1 - k) * DAY_MS for k in range(n_days)]
    sc, pc, fu, sh, sl = {}, {}, {}, {}, {}
    for j, c in enumerate(COINS):
        px, s = 100.0 * (j + 1), []
        for k in range(n_days):
            px *= 1 + drift + rng.gauss(0, 0.02 + 0.005 * j)
            s.append(px)
        sc[c], pc[c] = s, list(s)
        sh[c] = [x * 1.01 for x in s]
        sl[c] = [x * 0.99 for x in s]
        fu[c] = [0.0003 + rng.gauss(0, 0.0001) for _ in range(n_days)]
    return Panel(days, list(COINS), sc, pc, fu, sh, sl, "synthetic")


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.panel = synth_panel()
        self.clock = Clock(INC + DAY_MS + 20 * 60_000)
        self.perp_calls = 0
        self.led = h2.H2Ledger(self.root / "shadow_h2.sqlite")
        self.sh = self.make(self.led)

    def make(self, led, **kw):
        return h2.ShadowH2(led, panel_fn=self.panel_until, perp_fn=self.perp, exchange_fn=lambda: "binance", now_ms=self.clock, **kw)

    def tearDown(self):
        self.led.close()
        self.tmp.cleanup()

    def panel_until(self, d):
        if d not in self.panel.days:
            return None
        return self.panel.truncate(self.panel.days.index(d) + 1)

    def perp(self, ex, c, lo, hi):
        self.perp_calls += 1
        p = self.panel
        return [[d, 0, p.perp_close[c][k] * 1.005, 0, p.perp_close[c][k] * 1.0002, 0] for k, d in enumerate(p.days) if lo <= d <= hi]

    def run_to(self, day):
        self.clock.t = day + DAY_MS + 20 * 60_000
        return self.sh.tick()


class FrozenTests(_Base):
    def test_code_registry_and_hash_agree(self):
        self.assertEqual(h2.params_hash(h2.PARAMS), h2.FROZEN_SHA256)
        reg = json.loads(h2.registry_path().read_text(encoding="utf-8"))
        self.assertEqual(reg["params_sha256"], h2.FROZEN_SHA256)
        self.assertEqual(reg["params"], h2.PARAMS)
        self.assertEqual(h2.verify_frozen(), h2.FROZEN_SHA256)
        self.assertTrue(reg["registered_at"].startswith("2026-10-04T") and reg["registered_at"].endswith("+08:00"))

    def test_frozen_values_match_research_report(self):
        t, c, p = h2.PARAMS["trend"], h2.PARAMS["carry"], h2.PARAMS
        self.assertEqual((t["signal"], t["lookbacks"], t["portfolio_vol_target"], t["coin_cap"]), ("agree3", [20, 60, 120], 0.15, 0.10))
        self.assertEqual((t["regime"]["coin"], t["regime"]["ma_days"], t["regime"]["bear_mult"]), ("BTC", 200, 0.0))
        self.assertEqual(len(t["universe"]), 19)
        self.assertEqual((c["coins"], c["leverage"], c["mode"]), (["BTC", "ETH"], 3, "always"))
        self.assertEqual((p["combine"]["vol_window"], p["combine"]["sleeve_cost"]), (60, 0.0017))
        self.assertEqual((p["idle"]["rate"], p["benchmark"]["tbill"], p["gate"]["forward_days"]), (0.0399, 0.0399, 180))

    def test_changed_param_is_refused_and_writes_nothing(self):
        bad = copy.deepcopy(h2.PARAMS)
        bad["trend"]["coin_cap"] = 0.2
        with self.assertRaises(h2.FrozenParamsError):
            h2.verify_frozen(bad)
        sh = self.make(self.led, params=bad)
        self.clock.t = INC + DAY_MS + 20 * 60_000
        self.assertEqual(sh.tick(), [])
        self.assertIn("FROZEN PARAMS MISMATCH", sh.last_error)
        self.assertEqual(self.led.rows(), [])
        self.assertTrue(sh.refused)

    def test_tampered_registry_is_refused(self):
        reg = json.loads(h2.registry_path().read_text(encoding="utf-8"))
        reg["params"]["carry"]["leverage"] = 5
        p = self.root / "reg.json"
        p.write_text(json.dumps(reg), encoding="utf-8")
        sh = self.make(self.led, registry=p)
        self.clock.t = INC + DAY_MS + 20 * 60_000
        self.assertEqual(sh.tick(), [])
        self.assertIn("registry", sh.last_error)
        missing = self.make(self.led, registry=self.root / "nope.json")
        self.assertEqual(missing.tick(), [])
        self.assertIn("unreadable", missing.last_error)
        self.assertEqual(self.led.rows(), [])

    def test_ledger_with_other_stored_hash_is_refused(self):
        self.led.set_meta("params_sha256", "0" * 64)
        self.assertEqual(self.run_to(INC), [])
        self.assertIn("ledger stored hash", self.sh.last_error)
        self.assertFalse(self.sh.summary()["paramsFrozen"])

    def test_changed_cost_model_or_tbill_is_refused(self):
        with patch.object(h2, "CostModel", lambda: CostModel(taker=0.0004)):
            with self.assertRaises(h2.FrozenParamsError):
                h2.verify_frozen()
        with patch.object(stats, "TBILL", 0.05):
            with self.assertRaises(h2.FrozenParamsError):
                h2.verify_frozen()


class BookingTests(_Base):
    def test_inception_then_daily_three_legs_reconcile(self):
        got = self.run_to(INC)
        self.assertEqual(got, [INC])
        r0 = self.led.last()
        self.assertEqual(r0["inception"], 1)
        self.assertAlmostEqual(r0["w_trend"] + r0["w_carry"], 1.0, places=12)
        self.assertLess(r0["ret"], 0)  # entry costs only
        self.assertEqual(r0["pnl_idle"], 0.0)
        self.assertEqual(len(self.led.q("SELECT 1 FROM warmup WHERE sleeve='trend'")), 60)
        self.assertEqual(len(self.led.q("SELECT 1 FROM warmup WHERE sleeve='carry'")), 60)
        self.assertEqual(self.run_to(INC + 35 * DAY_MS), [])  # 35 days behind > catch-up limit 10: manual review
        self.assertIn("behind", self.sh.waiting)
        for k in (10, 20, 30, 35):
            self.run_to(INC + k * DAY_MS)
        rows = self.led.rows()
        self.assertEqual([r["day"] for r in rows], list(range(INC, INC + 36 * DAY_MS, DAY_MS)))
        for a, b in zip(rows, rows[1:]):
            legs = b["pnl_trend"] + b["pnl_idle"] + b["pnl_carry"] - b["cost_sleeve"]
            self.assertAlmostEqual(b["nav"] - a["nav"], legs, places=8)
            self.assertAlmostEqual(b["nav"], b["nav_trend"] + b["nav_carry"], places=8)
            self.assertAlmostEqual(b["pnl_idle"], a["nav_trend"] * max(0.0, 1 - json_gross(a)) * 0.0399 / 365, places=9)
            self.assertAlmostEqual(b["tbill_ret"], 0.0399 / 365)
        # monthly re-weighting on the first UTC day of November only
        reb = [ms_day(r["day"]) for r in rows if r["rebalanced"] and not r["inception"]]
        self.assertEqual(reb, ["2026-11-01"])
        nov = [r for r in rows if ms_day(r["day"]) == "2026-11-01"][0]
        self.assertGreaterEqual(nov["cost_sleeve"], 0.0)
        s = self.sh.summary()
        self.assertEqual(s["forwardDays"], 35)
        c = s["cumulative"]
        self.assertAlmostEqual(10000 * (1 + c["ret"]), 10000 + c["pnlTrend"] + c["pnlCarry"] + c["pnlIdle"] - c["costSleeve"]
                               + rows[0]["nav"] - 10000 - rows[0]["pnl_trend"] - rows[0]["pnl_carry"] - rows[0]["pnl_idle"], places=6)
        self.assertIn("进度 35/180", self.sh.digest_line())

    def test_summary_carries_every_field_the_performance_page_reads(self):
        for k in range(0, 4):
            self.run_to(INC + k * DAY_MS)
        s = self.sh.summary(rows=5)
        last, cum = s["last"], s["cumulative"]
        for key in ("dayStr", "ret", "nav", "pnl_trend", "pnl_carry", "pnl_idle", "cost_sleeve", "w_trend", "w_carry", "drawdown"):
            self.assertIn(key, last)
        for key in ("ret", "tbill", "excess", "pnlTrend", "pnlCarry", "pnlIdle", "costSleeve", "maxDrawdown"):
            self.assertTrue(math.isfinite(cum[key]), key)
        self.assertEqual(last["dayStr"], ms_day(INC + 3 * DAY_MS))
        self.assertEqual((s["progress"], s["forwardDays"], s["gate"]["forward_days"]), (3, 3, 180))
        self.assertAlmostEqual(cum["excess"], cum["ret"] - cum["tbill"], places=12)
        self.assertAlmostEqual(cum["tbill"], (1 + 0.0399 / 365) ** 3 - 1, places=12)
        self.assertEqual(s["abort"]["max_drawdown"], -0.05)
        self.assertIn("ci", s["gate"])
        json.dumps(s)  # the route serialises it as is

    def test_idempotent_and_waits_for_close_paper_and_data(self):
        self.clock.t = INC + DAY_MS + 5 * 60_000  # 08:05 BJ: bar closed < 15 min ago
        self.assertEqual(self.sh.tick(), [])
        self.assertEqual(self.led.rows(), [])
        sh = self.make(self.led, after_fn=lambda d: False)
        self.clock.t = INC + DAY_MS + 20 * 60_000
        self.assertEqual(sh.tick(), [])
        self.assertIn("paper rebalance", sh.waiting)
        sh = self.make(self.led, ready_fn=lambda d: (False, "BTC:1d"))
        self.assertEqual(sh.tick(), [])
        self.assertIn("BTC:1d", sh.waiting)
        self.assertEqual(self.sh.tick(), [INC])
        self.assertEqual(self.sh.tick(), [])
        calls = self.perp_calls
        self.assertEqual(self.sh.tick(), [])
        self.assertEqual(self.perp_calls, calls)
        with self.assertRaises(sqlite3.IntegrityError):
            self.led.tx(lambda db: db.execute("INSERT INTO days(day, computed_at, nav, ret, nav_trend, nav_carry, w_trend, w_carry, pnl_trend,"
                                              " pnl_idle, pnl_carry, cost_sleeve, rebalanced, ret_trend_sleeve, ret_carry_sleeve, idle_ret,"
                                              " gross_trend, tbill_ret, peak_nav, drawdown, state, targets, detail)"
                                              " VALUES (?,0,1,0,1,0,1,0,0,0,0,0,0,0,0,0,0,0,1,0,'{}','{}','{}')", (INC,)))

    def test_nothing_before_registered_inception(self):
        self.clock.t = INC + 20 * 60_000  # due day = 2026-10-03, before inception
        self.assertEqual(self.sh.tick(), [])
        self.assertIn("not closed yet", self.sh.waiting)
        self.assertIn("首个记录日 2026-10-04", self.sh.digest_line())

    def test_drawdown_abort_locks_verdict(self):
        p = self.panel
        i = p.days.index(INC + 3 * DAY_MS)
        for c in ("BTC", "ETH"):  # perp spikes 40%: 3x short perp leg is liquidated
            p.perp_close[c][i] *= 1.4
        self.run_to(INC)
        self.run_to(INC + 3 * DAY_MS)
        ev = [dict(e) for e in self.led.q("SELECT kind FROM events")]
        self.assertTrue(any(e["kind"] == "abort:carry_liquidation" for e in ev))
        self.assertTrue(any(e["kind"] == "abort:drawdown" for e in ev))
        self.assertTrue(self.sh.summary()["aborted"])
        self.assertIn("已触发中止条件", self.sh.digest_line())

    def test_evaluation_at_180_days(self):
        led = h2.H2Ledger(self.root / "e.sqlite")
        sh = self.make(led)
        rng = random.Random(1)
        cols = ["day", "computed_at", "exchange", "inception", "nav", "ret", "nav_trend", "nav_carry", "w_trend", "w_carry", "pnl_trend",
                "pnl_idle", "pnl_carry", "cost_sleeve", "rebalanced", "ret_trend_sleeve", "ret_carry_sleeve", "idle_ret", "gross_trend",
                "tbill_ret", "peak_nav", "drawdown", "state", "targets", "detail"]
        rows = []
        for k in range(181):
            r = dict.fromkeys(cols, 0)
            r.update(day=INC + k * DAY_MS, exchange="x", inception=int(k == 0), nav=1, ret=0.0006 + rng.gauss(0, 0.0003), peak_nav=1,
                     state=json.dumps({"month": ms_day(INC + k * DAY_MS)[:7], "trend_w": {}, "carry": {}}), targets="{}", detail="{}")
            rows.append(tuple(r[c] for c in cols))
        led.tx(lambda db: db.executemany(f"INSERT INTO days({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", rows))
        out = sh._maybe_evaluate()
        self.assertEqual(out["verdict"], "pass")
        self.assertGreater(out["excess_ci"][0], 0)
        self.assertIsNone(sh._maybe_evaluate())  # once only
        self.assertEqual(sh.summary()["evaluations"][0]["milestone"], 180)
        led.close()


def json_gross(row) -> float:
    return sum(abs(w) for w in json.loads(row["state"])["trend_w"].values())


class PureTests(unittest.TestCase):
    def test_sleeve_weights_inverse_vol_and_zero_vol_limit(self):
        rt = [0.001 * ((-1) ** k) for k in range(60)]
        rc = [0.0005 * ((-1) ** k) for k in range(60)]
        a, b = h2.sleeve_targets(rt, rc)
        self.assertAlmostEqual(a, 1 / 3, places=9)
        self.assertEqual(h2.sleeve_targets([0.0399 / 365] * 60, rc), (1.0, 0.0))
        self.assertEqual(h2.sleeve_targets(rt[:10], rc), (0.5, 0.5))
        self.assertEqual(h2.sleeve_targets([0.0] * 60, [0.0] * 60), (0.5, 0.5))

    def test_carry_step_matches_sim_a_and_fees_never_negative(self):
        st = h2.new_carry_state(100.0, 100.0)
        e0 = h2.carry_entry(st, 100.0, "BTC")
        N = 1 / (1 + 1 / 3)
        self.assertAlmostEqual(e0, N * (0.0011 + 0.0006))
        self.assertAlmostEqual(st["cash"] + st["q"] * 100 + st["E"], 1 - e0)
        ev = h2.carry_step(st, 100.0, 100.0, 100.0, 0.0003, "BTC")
        self.assertAlmostEqual(ev["ret"], N * 0.0003 / (1 - e0), places=12)
        ev = h2.carry_step(st, 80.0, 80.0, 80.0, 0.0, "BTC")  # price -20%: margin > 200% of initial -> resize
        self.assertTrue(ev["rebalanced"])
        self.assertGreater(ev["cost"], 0)
        ev = h2.carry_step(st, 120.0, 120.0, 140.0, 0.0, "BTC")  # high +75% vs last close: liquidated
        self.assertTrue(ev["liquidated"])
        self.assertGreater(st["q"], 0)  # always-on: re-entered at the close

    def test_trend_targets_caps_regime_and_causality(self):
        p = synth_panel(n_days=400)
        W = trend_b058.targets(p, h2.PARAMS["trend"])
        self.assertTrue(all(w <= 0.10 + 1e-15 and w >= 0 for c in p.coins for w in W[c]))
        n = 330
        cut = trend_b058.targets(p.truncate(n), h2.PARAMS["trend"])
        for c in p.coins:
            self.assertAlmostEqual(cut[c][-1], W[c][n - 1], places=14)
        bear = synth_panel(n_days=400, drift=-0.004)
        Wb = trend_b058.targets(bear, h2.PARAMS["trend"])
        btc = bear.spot_close["BTC"]
        for t in range(250, 400):
            if btc[t] <= sum(btc[t - 199:t + 1]) / 200:
                self.assertTrue(all(Wb[c][t] == 0 for c in bear.coins))


class IsolationTests(_Base):
    def test_never_writes_other_ledgers(self):
        others = {}
        for name in ("mainstream_strategy.sqlite", "mainstream_paper.sqlite", "mainstream.sqlite", "shadow_s3.sqlite", "exec_shadow.sqlite"):
            path = self.root / name
            db = sqlite3.connect(path)
            db.execute("CREATE TABLE t (x)")
            db.execute("INSERT INTO t VALUES (1)")
            db.commit()
            db.close()
            others[name] = hashlib.sha256(path.read_bytes()).hexdigest()
        opened = []
        real = sqlite3.connect

        def spy(path, *a, **k):
            opened.append(str(path))
            return real(path, *a, **k)

        with patch.dict(os.environ, {"AUU_DATA_DIR": str(self.root)}), patch.object(h2.sqlite3, "connect", spy):
            led = h2.H2Ledger()
            self.assertEqual(led.path, self.root / "shadow_h2.sqlite")
            sh = self.make(led)
            self.clock.t = INC + 5 * DAY_MS
            self.assertTrue(sh.tick())
            led.close()
        self.assertEqual(opened, [str(self.root / "shadow_h2.sqlite")])
        for name, digest in others.items():
            self.assertEqual(hashlib.sha256((self.root / name).read_bytes()).hexdigest(), digest, name)
        src = Path(h2.__file__).read_text() + Path(trend_b058.__file__).read_text()
        for bad in ("create_order", "place_order", "broker", "mainstream_strategy.sqlite", "mainstream_paper", "commit_day", "StrategyLedger("):
            self.assertNotIn(bad, src)
        tables = {r[0] for r in self.led.q("SELECT name FROM sqlite_master WHERE type='table'")} - {"sqlite_sequence"}
        self.assertEqual(tables, {"meta", "days", "warmup", "perp_daily", "events", "evaluations"})

    def test_digest_includes_h2_line(self):
        from app import alerts

        self.run_to(INC)
        with patch.object(h2, "peek_shadow", lambda: self.sh):
            extra = alerts._extra()
        key = [k for k in extra if k.startswith("H2 影子盘")]
        self.assertEqual(len(key), 1)
        self.assertIn("2026-10-04 当日", extra[key[0]])
        with patch.dict(os.environ, {"AUU_SHADOW_H2": "off"}), patch.object(h2, "peek_shadow", lambda: self.sh):
            self.assertFalse(any(k.startswith("H2") for k in alerts._extra()))


class ApiTests(unittest.TestCase):
    _ENV = ("AUU_AUTH", "AUU_ALLOW_SIGNUP", "AUU_USER_STORE", "AUU_USER_JOURNAL_DIR", "AUU_AUTH_RATE_MAX", "AUU_LEGACY_PUMP",
            "AUU_INVITE_CODE", "AUU_ADMIN_USER")

    def setUp(self):
        from app.auth.accounts import reset_accounts

        self.tmp = tempfile.TemporaryDirectory()
        self._prev = {k: os.environ.get(k) for k in self._ENV}
        root = Path(self.tmp.name)
        os.environ.update({"AUU_AUTH": "on", "AUU_ALLOW_SIGNUP": "on", "AUU_USER_STORE": str(root / "users.json"),
                           "AUU_USER_JOURNAL_DIR": str(root / "journals"), "AUU_AUTH_RATE_MAX": "1000", "AUU_LEGACY_PUMP": "off"})
        os.environ.pop("AUU_INVITE_CODE", None)
        os.environ.pop("AUU_ADMIN_USER", None)
        reset_accounts()
        self.led = h2.H2Ledger(root / "shadow_h2.sqlite")
        h2.reset_shadow(h2.ShadowH2(self.led, panel_fn=lambda d: None, perp_fn=lambda *a: [], exchange_fn=lambda: None))
        self._sock = patch.object(socket.socket, "connect", side_effect=AssertionError("network access in test"))
        self._sock.start()
        from fastapi.testclient import TestClient
        from app.main import create_app

        self.c = TestClient(create_app(legacy=False))

    def tearDown(self):
        from app.auth.accounts import reset_accounts

        self._sock.stop()
        h2.reset_shadow(None)
        self.led.close()
        for k, v in self._prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        reset_accounts()
        self.tmp.cleanup()

    def test_requires_login_then_shows_frozen_summary(self):
        self.assertEqual(self.c.get("/api/v1/mainstream/shadow/h2").status_code, 401)
        pw = "paperPass123"
        self.assertEqual(self.c.post("/api/v1/auth/register", json={"name": "vin", "password": pw, "password_confirm": pw}).status_code, 200)
        d = self.c.get("/api/v1/mainstream/shadow/h2").json()["data"]
        self.assertEqual(d["paramsSha256"], h2.FROZEN_SHA256)
        self.assertEqual(d["capital"], 0)
        self.assertTrue(d["paramsFrozen"])
        self.assertEqual(d["inceptionDay"], "2026-10-04")

    def _login(self):
        pw = "paperPass123"
        self.assertEqual(self.c.post("/api/v1/auth/register", json={"name": "vin", "password": pw, "password_confirm": pw}).status_code, 200)

    def test_no_ledger_yet_returns_registration_for_the_performance_page(self):
        empty = Path(self.tmp.name) / "empty"
        empty.mkdir()
        h2.reset_shadow(None)
        self._login()
        with patch.object(h2, "data_dir", lambda: empty):
            d = self.c.get("/api/v1/mainstream/shadow/h2").json()["data"]
        self.assertFalse((empty / h2.LEDGER_NAME).exists())  # reading never creates the ledger
        self.assertIsNone(d["ledger"])
        self.assertIsNone(d["last"])
        self.assertIsNone(d["cumulative"])
        self.assertEqual((d["inceptionDay"], d["progress"], d["capital"]), ("2026-10-04", 0, 0))
        self.assertEqual(d["gate"]["forward_days"], 180)
        self.assertEqual(d["abort"]["max_drawdown"], -0.05)
        self.assertEqual(d["paramsSha256"], h2.FROZEN_SHA256)
        self.assertEqual(h2.params_hash(h2.PARAMS), h2.FROZEN_SHA256)  # stub only reads the frozen params

    def test_empty_ledger_shows_registered_without_numbers(self):
        self._login()
        d = self.c.get("/api/v1/mainstream/shadow/h2").json()["data"]
        self.assertIsNone(d["last"])
        self.assertIsNone(d["cumulative"])
        self.assertEqual(d["progress"], 0)
        self.assertEqual(d["refused"], "")


if __name__ == "__main__":
    unittest.main()
