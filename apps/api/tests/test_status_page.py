"""Public status page (P1-6): minute uptime record, 30-day summary, coarse login-free payload."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.status import DAY, MIN, Uptime

T0 = 1_790_870_400_000  # 2026-10-02 00:00 BJ (UTC+8 midnight)

OK = {"data_fresh": True, "strategy_ok": True, "live_locked": True}


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


class UptimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = Clock(T0)
        self.u = Uptime(Path(self.tmp.name) / "u.sqlite", now_ms=self.clock)

    def tearDown(self):
        self.u.close()
        self.tmp.cleanup()

    def test_empty(self):
        s = self.u.summary()
        self.assertIsNone(s["upPct"])
        self.assertEqual(s["measuredMin"], 0)

    def test_full_uptime_measured_only_since_first_record(self):
        for i in range(120):
            self.u.record(OK, T0 + i * MIN)
        self.clock.t = T0 + 120 * MIN + 5_000
        s = self.u.summary()
        self.assertEqual(s["since"], T0)
        self.assertEqual(s["measuredMin"], 120)
        self.assertEqual(s["upPct"], 1.0)
        self.assertEqual(s["healthyPct"], 1.0)
        self.assertEqual(s["outages"], [])
        self.assertEqual([d["day"] for d in s["days"]], ["2026-10-02"])

    def test_gap_is_downtime_and_unhealthy_run_is_degraded(self):
        for i in range(100):
            if 40 <= i < 50:
                continue  # API down 10 min
            bad = 70 <= i < 76  # stale data 6 min
            self.u.record({**OK, "data_fresh": not bad}, T0 + i * MIN)
        self.clock.t = T0 + 100 * MIN
        s = self.u.summary()
        self.assertEqual(s["measuredMin"], 100)
        self.assertAlmostEqual(s["upPct"], 0.9)
        self.assertAlmostEqual(s["healthyPct"], 0.84)
        self.assertEqual(s["outages"], [
            {"start": T0 + 70 * MIN, "minutes": 6, "kind": "degraded"},
            {"start": T0 + 40 * MIN, "minutes": 10, "kind": "down"},
        ])

    def test_trailing_downtime_counts(self):
        for i in range(30):
            self.u.record(OK, T0 + i * MIN)
        self.clock.t = T0 + 60 * MIN  # nothing written for the last 30 min
        s = self.u.summary()
        self.assertAlmostEqual(s["upPct"], 0.5)
        self.assertEqual(s["outages"][0], {"start": T0 + 30 * MIN, "minutes": 30, "kind": "down"})

    def test_worst_probe_wins_within_a_minute_and_window_is_30_days(self):
        self.u.record(OK, T0)
        self.u.record({**OK, "live_locked": False}, T0 + 10_000)
        self.u.record(OK, T0 + 20_000)
        self.clock.t = T0 + MIN
        self.assertEqual(self.u.summary()["healthyPct"], 0.0)
        # 40 days later the old minute is outside the window
        self.u.record(OK, T0 + 40 * DAY)
        self.clock.t = T0 + 40 * DAY + MIN
        s = self.u.summary()
        self.assertEqual(s["since"], T0 + 40 * DAY - 30 * DAY + MIN)
        self.assertLess(s["upPct"], 0.001)
        self.assertEqual(len(s["days"]), 31)

    def test_prunes_after_90_days(self):
        self.u.record(OK, T0)
        self.u.record(OK, T0 + 91 * DAY)
        n = self.u._db.execute("SELECT COUNT(*) FROM minutes").fetchone()[0]
        self.assertEqual(n, 1)


class PublicPayloadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        from app import status

        self.u = Uptime(Path(self.tmp.name) / "u.sqlite")
        status.reset_uptime(self.u)

    def tearDown(self):
        from app import status

        status.reset_uptime(None)
        self.u.close()
        self.tmp.cleanup()

    def _fake(self):
        md = {"enabled": True, "stale": False, "staleSeries": [], "blocked": {"binance": "451 restricted"}, "exchanges": ["binance", "okx"],
              "lastRefreshMs": 1, "lastError": "HTTP 451 from https://api.binance.com", "series": {"BTC:1m": {}}, "symbols": ["BTC"]}
        st = {"active": True, "strategy": "trend_tsmom_v1", "lastDay": "2026-10-02", "lastRunAt": 2, "hoursSinceRebalance": 17.0,
              "stallHours": 26, "stalled": False, "lastError": "Traceback /opt/auu/apps/api/x.py"}
        live = {"liveEnabled": False, "pubkey": "SECRETPUB", "keypairRelpath": "keys/k.json"}
        return patch.multiple("app.routes.health", _mainstream_fields=lambda: md, _strategy_status=lambda: st, _live_fields=lambda: live)

    def test_payload_is_coarse(self):
        from app.status import public_status

        with self._fake():
            p = public_status()
        self.assertTrue(p["healthy"])
        self.assertEqual(p["liveTrading"], "locked")
        self.assertEqual(p["marketData"]["exchangesBlocked"], 1)
        self.assertEqual(p["strategy"]["lastRebalanceDay"], "2026-10-02")
        blob = json.dumps(p)
        for leak in ("binance", "451", "/opt", "SECRETPUB", "keys/", "Traceback", "trend_tsmom", "version", "nav", "BTC"):
            self.assertNotIn(leak, blob)

    def test_route_is_public_with_auth_on(self):
        from fastapi.testclient import TestClient

        from app.main import create_app

        with self._fake(), patch.dict(os.environ, {"AUU_AUTH": "on", "AUU_UPTIME": "off", "AUU_RECON": "off", "AUU_ALERTS": "off"}):
            with TestClient(create_app(legacy=False)) as c:
                r = c.get("/api/v1/status")
                self.assertEqual(r.status_code, 200, r.text)
                self.assertEqual(r.json()["data"]["api"], "up")
                self.assertEqual(c.get("/api/v1/mainstream/paper/account").status_code, 401)


if __name__ == "__main__":
    unittest.main()
