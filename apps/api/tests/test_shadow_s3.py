"""S3 shadow hypothesis record: frozen rule, out-of-sample only, no capital, evaluation at 100 trades."""
from __future__ import annotations

import json
import os
import random
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.backtest.costs import CostModel
from app.backtest.panel import DAY_MS, day_ms
from app.paper import shadow_s3 as s3

H = s3.HOUR_MS
T0 = day_ms("2026-10-03")  # registration time
_ENV = ("AUU_AUTH", "AUU_ALLOW_SIGNUP", "AUU_USER_STORE", "AUU_USER_JOURNAL_DIR", "AUU_AUTH_RATE_MAX", "AUU_LEGACY_PUMP",
        "AUU_INVITE_CODE", "AUU_ADMIN_USER", "AUU_DATA_DIR")
PW = "paperPass123"


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = Clock(T0)
        self.led = s3.ShadowLedger(Path(self.tmp.name) / "shadow_s3.sqlite", now_ms=self.clock)
        self.fund: dict[str, list[tuple[int, float]]] = {}
        self.px: dict[tuple[str, int], float] = {}
        self.sh = s3.ShadowS3(self.led, coins_fn=lambda: sorted(self.fund), funding_fn=self.funding, price_fn=self.price, now_ms=self.clock)

    def tearDown(self):
        self.led.close()
        self.tmp.cleanup()

    def funding(self, c, since):
        return [(t, r) for t, r in self.fund.get(c, []) if t >= since and t <= self.clock.t]  # only what has settled

    def price(self, c, bar):
        return self.px.get((c, bar)) if bar + H <= self.clock.t else None

    def series(self, c, start, n, every_h=8, rate=0.0001, spikes=None):
        spikes = spikes or {}
        self.fund[c] = [(start + k * every_h * H, spikes.get(k, rate)) for k in range(n)]

    def trades(self):
        return [dict(r) for r in self.led.q("SELECT * FROM trades ORDER BY id")]


class RuleTests(_Base):
    def test_signal_entry_exit_and_net_of_costs(self):
        self.series("SOL", T0 - 2 * DAY_MS, 30, spikes={3: -0.002, 6: -0.0012, 8: -0.0015, 9: -0.0009, 20: 0.0003})
        # settlement 3 is before registration: ignored (out-of-sample only)
        sig = T0 - 2 * DAY_MS + 6 * 8 * H  # = T0 (registration instant counts)
        bar = sig // H * H
        self.px[("SOL", bar)] = 100.0
        self.px[("SOL", bar + 72 * H)] = 110.0
        self.clock.t = sig + 10 * 60_000
        out = self.sh.tick()
        self.assertEqual(out["signals"], 1)
        t = self.trades()
        self.assertEqual(len(t), 1)
        self.assertEqual((t[0]["signal_ts"], t[0]["entry_bar"], t[0]["exit_bar"], t[0]["status"]), (sig, bar, bar + 72 * H, "pending"))
        self.assertAlmostEqual(t[0]["f8"], -0.0012)
        self.clock.t = bar + H + s3.SETTLE_MS
        self.sh.tick()
        self.assertEqual(self.trades()[0]["status"], "open")
        self.assertEqual(self.trades()[0]["entry_px"], 100.0)
        # settlement 8 (-0.15%) falls inside the open trade: skipped, not stacked
        self.clock.t = bar + 73 * H + s3.SETTLE_MS
        self.sh.tick()
        t = self.trades()
        self.assertEqual(len(t), 1)
        self.assertEqual(t[0]["status"], "closed")
        fund = sum(r for ts, r in self.fund["SOL"] if bar < ts // H * H <= bar + 72 * H)
        cm = CostModel()
        cost = 2 * (cm.taker + cm.slippage["SOL"])
        self.assertAlmostEqual(t[0]["funding"], fund)
        self.assertAlmostEqual(t[0]["cost"], cost)
        self.assertAlmostEqual(t[0]["net"], 0.10 - fund - cost)
        self.assertLess(fund, 0)  # longs receive negative funding

    def test_interval_is_inferred_from_settlement_spacing(self):
        # 4h coin: -0.06% per 4h = -0.12%/8h -> signal; an 8h coin at -0.06% is not
        self.series("ARB", T0, 10, every_h=4, rate=0.00005, spikes={4: -0.0006})
        self.series("LINK", T0, 10, every_h=8, rate=0.00005, spikes={4: -0.0006})
        self.clock.t = T0 + 3 * DAY_MS
        self.sh.tick()
        t = self.trades()
        self.assertEqual([x["coin"] for x in t], ["ARB"])
        self.assertEqual(t[0]["interval_h"], 4.0)
        self.assertAlmostEqual(t[0]["f8"], -0.0012)

    def test_positive_extremes_and_old_settlements_never_trade(self):
        self.series("DOGE", T0 - 10 * DAY_MS, 60, spikes={1: -0.005, 40: 0.004})
        self.clock.t = T0 + 12 * DAY_MS
        self.sh.tick()
        self.assertEqual(self.trades(), [])

    def test_missing_price_voids_after_48h_and_frees_the_coin(self):
        self.series("TRX", T0, 30, spikes={0: -0.002, 2: -0.002})
        self.clock.t = T0 + H + s3.SETTLE_MS
        self.sh.tick()
        self.clock.t = T0 + 50 * H + s3.SETTLE_MS
        self.sh.tick()
        t = self.trades()
        self.assertEqual(t[0]["status"], "void")

    def test_matches_the_report_loop_on_a_random_tape(self):
        rnd = random.Random(4)
        start = T0
        for c in ("BTC", "ETH", "AVAX"):
            spikes = {k: -rnd.choice([0.0011, 0.002, 0.0008]) for k in range(0, 300) if rnd.random() < 0.08}
            self.series(c, start, 300, spikes=spikes)
            p = 100.0
            for h in range(300 * 8 + 200):
                p *= 1 + rnd.gauss(0, 0.004)
                self.px[(c, start + h * H)] = p
        self.clock.t = start
        while self.clock.t < start + 310 * 8 * H:
            self.clock.t += 6 * H
            self.sh.tick()
        got = {(t["coin"], t["entry_bar"]): t["net"] for t in self.trades() if t["status"] == "closed"}
        # transcription of hourly.py s3_funding(kind="abs", thr=0.001, hold=72) long-only + trade()
        want = {}
        cm = CostModel()
        for c, rows in self.fund.items():
            busy = -1
            for ts, rate in rows:
                i = ts // H * H
                f8 = rate * 8 / 8
                if i <= busy or not (f8 <= -0.001):
                    continue
                i1 = i + 72 * H
                if i1 > start + 300 * 8 * H - 24 * H:
                    continue
                p0, p1 = self.px[(c, i)], self.px[(c, i1)]
                fund = sum(r for t2, r in rows if i < t2 // H * H <= i1)
                want[(c, i)] = (p1 / p0 - 1) - fund - 2 * (cm.taker + cm.slippage.get(c, cm.slippage_default))
                busy = i1
        common = {k for k in want if k in got}
        self.assertGreater(len(common), 20)
        self.assertEqual({k for k in got if k[1] + 72 * H <= start + 300 * 8 * H - 24 * H}, set(want))
        for k in common:
            self.assertAlmostEqual(got[k], want[k], places=12)


class EvaluationTests(_Base):
    def test_evaluation_runs_once_at_100_closed_trades(self):
        coins = [f"C{k:02d}" for k in range(25)]
        for c in coins:  # 5 signals per coin, 4 days apart -> 125 trades
            self.series(c, T0, 70, spikes={k: -0.0011 for k in (0, 12, 24, 36, 48)})
            for k in (0, 12, 24, 36, 48):
                b = T0 + k * 8 * H
                self.px[(c, b)] = 100.0
                self.px[(c, b + 72 * H)] = 101.0 + (hash((c, k)) % 7) - 3
        done = []
        while self.clock.t < T0 + 30 * DAY_MS:
            self.clock.t += 6 * H
            out = self.sh.tick()
            if out.get("evaluated"):
                done.append((len(self.sh.closed()), out["evaluated"]))
        self.assertEqual(len(done), 1)
        n_at, ev = done[0]
        self.assertGreaterEqual(n_at, 100)
        self.assertEqual(ev["n"], 100)
        self.assertEqual(ev, {**s3.evaluate(self.sh.closed()[:100]), "milestone": 100, "verdict": ev["verdict"]})
        self.assertIn(ev["verdict"], ("candidate", "fail"))
        s = self.sh.summary()
        self.assertEqual(len(s["evaluations"]), 1)
        self.assertEqual(s["progress"], 100)
        self.assertEqual(s["label"], "影子假设，非证据，需 ≥100 笔新交易再评估")
        self.assertEqual(s["capital"], 0)

    def test_summary_before_100_is_labelled_and_frozen(self):
        s = self.sh.summary()
        self.assertTrue(s["ruleFrozen"])
        self.assertEqual(s["rule"]["threshold_f8"], -0.001)
        self.assertEqual(s["rule"]["hold_hours"], 72)
        self.assertEqual(s["evaluations"], [])
        self.assertTrue(any("0.05%" in n for n in s["notes"]))

    def test_ledger_is_separate_and_has_no_order_path(self):
        tables = {r[0] for r in self.led.q("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertEqual(tables - {"sqlite_sequence"}, {"meta", "cursor", "trades", "evaluations"})
        src = Path(s3.__file__).read_text()
        for bad in ("create_order", "broker", "mainstream_paper", "mainstream_strategy.sqlite", "place_order"):
            self.assertNotIn(bad, src)


class ShadowApiTests(unittest.TestCase):
    def setUp(self):
        from app.auth.accounts import reset_accounts

        self.tmp = tempfile.TemporaryDirectory()
        self._prev = {k: os.environ.get(k) for k in _ENV}
        root = Path(self.tmp.name)
        os.environ.update({"AUU_AUTH": "on", "AUU_ALLOW_SIGNUP": "on", "AUU_USER_STORE": str(root / "users.json"),
                           "AUU_USER_JOURNAL_DIR": str(root / "journals"), "AUU_AUTH_RATE_MAX": "1000", "AUU_LEGACY_PUMP": "off"})
        os.environ.pop("AUU_INVITE_CODE", None)
        os.environ.pop("AUU_ADMIN_USER", None)
        reset_accounts()
        self.led = s3.ShadowLedger(root / "shadow_s3.sqlite")
        s3.reset_shadow(s3.ShadowS3(self.led, coins_fn=lambda: [], funding_fn=lambda c, s: [], price_fn=lambda c, b: None))
        self._sock = patch.object(socket.socket, "connect", side_effect=AssertionError("network access in test"))
        self._sock.start()
        from fastapi.testclient import TestClient
        from app.main import create_app

        self.TestClient = TestClient
        self.app = create_app(legacy=False)

    def tearDown(self):
        from app.auth.accounts import reset_accounts

        self._sock.stop()
        s3.reset_shadow(None)
        self.led.close()
        for k, v in self._prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        reset_accounts()
        self.tmp.cleanup()

    def test_unauthenticated_shadow_view_returns_401(self):
        r = self.TestClient(self.app).get("/api/v1/mainstream/shadow/s3")
        self.assertEqual(r.status_code, 401, r.text)
        self.assertEqual(r.json()["error"]["code"], "AUTH_REQUIRED")
        bad = self.TestClient(self.app, cookies={"auu_session": "forged.9999999999.sig"})
        self.assertEqual(bad.get("/api/v1/mainstream/shadow/s3").status_code, 401)

    def test_route_rechecks_session_even_without_gate(self):
        from starlette.requests import Request
        from app.routes import mainstream_shadow

        req = Request({"type": "http", "method": "GET", "path": "/", "headers": [], "query_string": b""})
        self.assertEqual(mainstream_shadow.shadow_s3(req).status_code, 401)

    def test_logged_in_view(self):
        c = self.TestClient(self.app)
        self.assertEqual(c.post("/api/v1/auth/register", json={"name": "vin", "password": PW, "password_confirm": PW}).status_code, 200)
        d = c.get("/api/v1/mainstream/shadow/s3").json()["data"]
        self.assertEqual(d["label"], "影子假设，非证据，需 ≥100 笔新交易再评估")
        self.assertEqual(d["counts"]["closed"], 0)
        self.assertEqual(d["evalAt"], 100)


if __name__ == "__main__":
    unittest.main()
