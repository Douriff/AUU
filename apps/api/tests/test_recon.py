"""Cross-source daily close reconciliation: flags, alerts once, never switches source, no network."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.marketdata.mainstream.recon import DAY_MS, Reconciler, fetch_daily_closes, thresholds
from app.marketdata.mainstream.store import MarketStore

TODAY = 1_790_985_600_000  # 2026-10-03 00:00 UTC
NOW = TODAY + 3 * 3_600_000
COINS = ["BTC", "ETH", "DOGE"]
PX = {"BTC": 80_000.0, "ETH": 3_000.0, "DOGE": 0.2}


class ReconTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.store = MarketStore(d / "m.sqlite")
        for c in COINS:
            self.store.upsert_candles("binance", c, "1d", [[TODAY - i * DAY_MS, 1, 1, 1, PX[c], 1] for i in range(0, 10)])
        self.svc = SimpleNamespace(cfg=SimpleNamespace(strategy_symbols=COINS, quote="USDT", all_symbols=lambda: COINS),
                                   store=self.store, exchange_for_read=lambda: "binance")
        self.remote = {c: {TODAY - i * DAY_MS: PX[c] * 1.0005 for i in range(0, 9)} for c in COINS}
        self.calls: list = []
        self.alerts: list = []
        self.t = {"now": NOW}

        def fetch(venue, coin, quote, limit):
            self.calls.append((venue, coin, quote, limit))
            if isinstance(self.remote.get(coin), Exception):
                raise self.remote[coin]
            return dict(self.remote[coin])

        self.rec = Reconciler(d / "recon.sqlite", svc_fn=lambda: self.svc, fetch=fetch,
                              alert_fn=lambda *a: self.alerts.append(a) or "sent", now_ms=lambda: self.t["now"], sleep=lambda s: None)

    def tearDown(self):
        self.rec.close()
        self.store.close()
        self.tmp.cleanup()

    def test_all_ok_no_alert_and_secondary_is_other_venue(self):
        out = self.rec.run_once()
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["secondary"], "okx")
        self.assertEqual(out["checked"], len(COINS) * self.rec.days)
        self.assertEqual(out["flagged"], 0)
        self.assertEqual(self.alerts, [])
        self.assertEqual({c[0] for c in self.calls}, {"okx"})
        self.assertEqual(len(self.calls), len(COINS))  # one GET per coin
        s = self.rec.summary()
        self.assertFalse(s["autoSwitch"])
        self.assertEqual(len(s["coins"]), len(COINS))
        self.assertAlmostEqual(s["coins"][0]["latestDev"], 0.05, places=6)
        self.assertNotIn(TODAY, [r["day"] for r in self.rec._db.execute("SELECT day FROM checks")])  # forming bar never compared

    def test_deviation_and_missing_alert_once(self):
        self.remote["DOGE"][TODAY - DAY_MS] = 0.2 * 1.02
        del self.remote["ETH"][TODAY - 2 * DAY_MS]
        out = self.rec.run_once()
        self.assertEqual(out["flagged"], 2)
        self.assertEqual(out["maxCoin"], "DOGE")
        self.assertEqual(len(self.alerts), 1)
        key, kind, subject, body = self.alerts[0]
        self.assertEqual(kind, "recon")
        self.assertIn("DOGE", body)
        self.assertIn("对照源缺这一天", body)
        self.assertIn("没有自动切换", body)
        # next day: the same findings are not mailed again
        self.t["now"] = NOW + DAY_MS
        self.rec.run_once()
        self.assertEqual(len(self.alerts), 1)
        self.assertEqual(self.svc.exchange_for_read(), "binance")  # source untouched
        flags = self.rec.summary()["flags"]
        self.assertEqual({f["coin"] for f in flags}, {"DOGE", "ETH"})

    def test_per_coin_threshold(self):
        self.remote["DOGE"][TODAY - DAY_MS] = 0.2 * 1.008
        with patch.dict(os.environ, {"AUU_RECON_THRESHOLDS": "DOGE:1.0"}):
            self.assertEqual(thresholds()[1], {"DOGE": 1.0})
            out = self.rec.run_once()
        self.assertEqual(out["flagged"], 0)
        out = self.rec.run_once()
        self.assertEqual(out["flagged"], 1)

    def test_venue_error_is_not_a_finding_and_retries_bounded(self):
        for c in COINS:
            self.remote[c] = RuntimeError("down")
        self.assertTrue(self.rec.due())
        out = self.rec.run_once()
        self.assertEqual(out["status"], "error")
        self.assertEqual(self.alerts, [])
        self.assertFalse(self.rec.due())  # retry waits retry_min
        self.t["now"] += 31 * 60_000
        self.assertTrue(self.rec.due())
        self.rec.tick()
        self.t["now"] += 31 * 60_000
        self.rec.tick()
        self.t["now"] += 31 * 60_000
        self.assertFalse(self.rec.due())  # 3 tries per day at most
        self.t["now"] = NOW + DAY_MS
        self.assertTrue(self.rec.due())

    def test_due_after_close_once_a_day(self):
        self.t["now"] = TODAY + 5 * 60_000
        self.assertFalse(self.rec.due())  # before 00:20 UTC
        self.t["now"] = NOW
        self.assertIsNotNone(self.rec.tick())
        self.assertIsNone(self.rec.tick())
        self.assertEqual(self.rec.health()["status"], "ok")

    def test_rest_parsers(self):
        bn = [[TODAY - DAY_MS, "1", "1", "1", "80000.5", "1"], [TODAY, "1", "1", "1", "81000", "1"]]
        okx = {"code": "0", "data": [[str(TODAY), "1", "1", "1", "81001", "1"], [str(TODAY - DAY_MS), "1", "1", "1", "80001", "1"]]}
        urls = []
        got = fetch_daily_closes("binance", "BTC", "USDT", 9, get=lambda u: urls.append(u) or bn)
        self.assertEqual(got[TODAY - DAY_MS], 80000.5)
        got = fetch_daily_closes("okx", "BTC", "USDT", 9, get=lambda u: urls.append(u) or okx)
        self.assertEqual(got[TODAY - DAY_MS], 80001.0)
        self.assertIn("symbol=BTCUSDT&interval=1d", urls[0])
        self.assertIn("instId=BTC-USDT&bar=1Dutc", urls[1])
        with self.assertRaises(RuntimeError):
            fetch_daily_closes("okx", "BTC", "USDT", 9, get=lambda u: {"code": "51001", "msg": "x"})


class ReconRouteTests(unittest.TestCase):
    def test_route_requires_login_and_health_has_recon(self):
        from fastapi.testclient import TestClient

        from app.main import create_app

        with patch.dict(os.environ, {"AUU_AUTH": "on"}):
            c = TestClient(create_app(legacy=False))
            self.assertEqual(c.get("/api/v1/mainstream/recon").status_code, 401)
            h = c.get("/api/v1/health").json()["data"]
        self.assertIn("recon", h)
        self.assertFalse(h["recon"].get("autoSwitch", False))


if __name__ == "__main__":
    unittest.main()
