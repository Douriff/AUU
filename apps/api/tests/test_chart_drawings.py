"""Chart drawings: validation, store (isolation, concurrency base, limits) and login-gated routes."""
from __future__ import annotations

import os
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import chart_drawings as cd

T0 = 1_760_000_000_000


def seg(v=100.0, t=T0):
    return {"name": "segment", "points": [{"timestamp": t, "value": v}, {"timestamp": t + 3_600_000, "value": v + 5}]}


class ValidateTests(unittest.TestCase):
    def test_accepts_all_tools_and_trims(self):
        for name in cd.ALLOWED_TOOLS:
            out = cd.validate_drawings([{"name": name, "points": [{"timestamp": T0, "value": 1}], "extendData": "x" * 500}])
            self.assertEqual(out[0]["name"], name)
            self.assertLessEqual(len(out[0]["extendData"]), cd.MAX_TEXT)

    def test_advanced_tools_whitelisted_and_legacy_kept(self):
        legacy = {"segment", "rayLine", "horizontalStraightLine", "verticalStraightLine", "fibonacciLine", "auuRect", "parallelStraightLine", "auuText"}
        adv = {"auuFibExt", "auuPitchfork", "auuFibChannel", "auuMeasure", "auuPriceRange", "auuDateRange"}
        self.assertTrue(legacy <= cd.ALLOWED_TOOLS, "step 1-2 tools must stay valid (stored documents)")
        self.assertTrue(adv <= cd.ALLOWED_TOOLS)
        self.assertEqual(cd.ALLOWED_TOOLS, legacy | adv)  # nothing else sneaks in

    def test_three_point_advanced_tool_roundtrip(self):
        pts = [{"timestamp": T0 + i * 3_600_000, "value": 100.0 + i} for i in range(3)]
        out = cd.validate_drawings([{"name": "auuPitchfork", "points": pts}, {"name": "auuMeasure", "points": pts[:2]}])
        self.assertEqual([d["name"] for d in out], ["auuPitchfork", "auuMeasure"])
        self.assertEqual(len(out[0]["points"]), 3)

    def test_unknown_tool_still_rejected(self):
        for name in ("auuFibExtX", "eval", "", None, "AUUMEASURE", "brush"):
            with self.assertRaises(cd.DrawingsError):
                cd.validate_drawings([{"name": name, "points": [{"timestamp": T0, "value": 1}]}])

    def test_rejects_bad_payloads(self):
        bad = [
            "x",
            [1],
            [{"name": "evil", "points": [{"timestamp": T0, "value": 1}]}],
            [{"name": "segment", "points": []}],
            [{"name": "segment", "points": [{"timestamp": T0, "value": 1}] * 6}],
            [{"name": "segment", "points": [{"timestamp": T0, "value": float("nan")}]}],
            [{"name": "segment", "points": [{"timestamp": T0, "value": float("inf")}]}],
            [{"name": "segment", "points": [{"timestamp": "1", "value": 1}]}],
            [{"name": "segment", "points": [{"timestamp": True, "value": 1}]}],
            [{"name": "segment", "points": [{"timestamp": 5, "value": 1}]}],
            [{"name": "segment", "points": [{"timestamp": T0, "value": 1e15}]}],
            [{"name": "auuText", "points": [{"timestamp": T0, "value": 1}], "extendData": {"a": 1}}],
            [{"name": "segment", "points": [{"timestamp": T0, "value": 1}], "styles": {"a": "x" * 100}}],
            [{"name": "segment", "points": [{"timestamp": T0, "value": 1}], "styles": [1]}],
        ]
        for b in bad:
            with self.assertRaises(cd.DrawingsError, msg=repr(b)[:80]):
                cd.validate_drawings(b)

    def test_limits(self):
        with self.assertRaises(cd.DrawingsError):
            cd.validate_drawings([seg()] * (cd.MAX_DRAWINGS + 1))
        big = [{"name": "segment", "points": [{"timestamp": T0, "value": 1.2345678901234}], "styles": {f"k{i}": "v" * 60 for i in range(24)}}] * 200
        with self.assertRaises(cd.DrawingsError):
            cd.validate_drawings(big)

    def test_symbol_tf(self):
        self.assertEqual(cd.normalize_symbol("btc/usdt"), "BTC")
        self.assertEqual(cd.normalize_symbol("1000PEPE"), "1000PEPE")
        for s in ("", "../x", "a" * 30, None):
            with self.assertRaises(cd.DrawingsError):
                cd.normalize_symbol(s)
        self.assertEqual(cd.normalize_tf(None), "")
        self.assertEqual(cd.normalize_tf("1h"), "1h")
        with self.assertRaises(cd.DrawingsError):
            cd.normalize_tf("2h")


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.s = cd.DrawingsStore(Path(self.tmp.name) / "chart_drawings.sqlite")

    def tearDown(self):
        self.s.close()
        self.tmp.cleanup()

    def test_roundtrip_isolation_and_delete(self):
        self.assertEqual(self.s.get("u1", "BTC")["updatedAt"], 0)
        r = self.s.put("u1", "BTC", "", [seg()], now_ms=1000)
        self.assertEqual(r["updatedAt"], 1000)
        self.assertEqual(self.s.get("u1", "BTC")["drawings"][0]["name"], "segment")
        self.assertEqual(self.s.get("u2", "BTC")["drawings"], [])  # other user
        self.assertEqual(self.s.get("u1", "ETH")["drawings"], [])  # other symbol
        self.assertEqual(self.s.get("u1", "BTC", "1h")["drawings"], [])  # other tf
        self.assertEqual(self.s.list_charts("u1")[0]["count"], 1)
        self.s.put("u1", "BTC", "", [])
        self.assertEqual(self.s.get("u1", "BTC")["updatedAt"], 0)

    def test_base_conflict_and_monotonic(self):
        a = self.s.put("u1", "BTC", "", [seg()], now_ms=5000)
        b = self.s.put("u1", "BTC", "", [seg(1)], base_updated_at=a["updatedAt"], now_ms=4000)  # clock went back
        self.assertGreater(b["updatedAt"], a["updatedAt"])
        with self.assertRaises(cd.Conflict) as c:
            self.s.put("u1", "BTC", "", [seg(2)], base_updated_at=a["updatedAt"])
        self.assertEqual(c.exception.current["updatedAt"], b["updatedAt"])
        # base 0 = "I think nothing is stored": only valid when empty
        with self.assertRaises(cd.Conflict):
            self.s.put("u1", "BTC", "", [seg(3)], base_updated_at=0)
        self.s.put("u1", "ETH", "", [seg()], base_updated_at=0)

    def test_per_user_chart_cap(self):
        with patch.object(cd, "MAX_KEYS_PER_USER", 3):
            for sym in ("A", "B", "C"):
                self.s.put("u1", sym, "", [seg()])
            with self.assertRaises(cd.DrawingsError):
                self.s.put("u1", "D", "", [seg()])
            self.s.put("u1", "A", "", [seg(9)])  # update of existing still fine
            self.s.put("u2", "D", "", [seg()])  # other users unaffected

    def test_rate_limiter(self):
        rl = cd.RateLimiter(3, 10)
        self.assertTrue(all(rl.allow("u", now=t) for t in (0, 1, 2)))
        self.assertFalse(rl.allow("u", now=3))
        self.assertTrue(rl.allow("other", now=3))
        self.assertTrue(rl.allow("u", now=11))


class RouteTests(unittest.TestCase):
    _ENV = ("AUU_AUTH", "AUU_ALLOW_SIGNUP", "AUU_USER_STORE", "AUU_USER_JOURNAL_DIR", "AUU_AUTH_RATE_MAX", "AUU_LEGACY_PUMP",
            "AUU_INVITE_CODE", "AUU_ADMIN_USER", "AUU_ROLE")

    def setUp(self):
        from app.auth.accounts import reset_accounts

        self.tmp = tempfile.TemporaryDirectory()
        self._prev = {k: os.environ.get(k) for k in self._ENV}
        root = Path(self.tmp.name)
        os.environ.update(
            {
                "AUU_AUTH": "on",
                "AUU_ALLOW_SIGNUP": "on",
                "AUU_USER_STORE": str(root / "users.json"),
                "AUU_USER_JOURNAL_DIR": str(root / "journals"),
                "AUU_AUTH_RATE_MAX": "1000",
                "AUU_LEGACY_PUMP": "off",
            }
        )
        for k in ("AUU_INVITE_CODE", "AUU_ADMIN_USER", "AUU_ROLE"):
            os.environ.pop(k, None)
        reset_accounts()
        self.store = cd.DrawingsStore(root / "chart_drawings.sqlite")
        cd.reset_store(self.store)
        cd.WRITE_LIMIT.reset()
        cd.READ_LIMIT.reset()
        self._sock = patch.object(socket.socket, "connect", side_effect=AssertionError("network access in test"))
        self._sock.start()
        from fastapi.testclient import TestClient

        from app.main import create_app

        self.c = TestClient(create_app(legacy=False))

    def tearDown(self):
        from app.auth.accounts import reset_accounts

        self._sock.stop()
        cd.reset_store(None)
        self.store.close()
        for k, v in self._prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        reset_accounts()
        self.tmp.cleanup()

    def _login(self, name="vin"):
        pw = "paperPass123"
        r = self.c.post("/api/v1/auth/register", json={"name": name, "password": pw, "password_confirm": pw})
        self.assertEqual(r.status_code, 200, r.text)

    def test_login_required(self):
        self.assertEqual(self.c.get("/api/v1/chart/drawings?symbol=BTC").status_code, 401)
        self.assertEqual(self.c.put("/api/v1/chart/drawings", json={"symbol": "BTC", "drawings": []}).status_code, 401)
        self.assertEqual(self.c.get("/api/v1/chart/drawings/index").status_code, 401)

    def test_put_get_conflict_and_validation(self):
        self._login()
        r = self.c.get("/api/v1/chart/drawings?symbol=btc")
        self.assertEqual(r.json()["data"], {"symbol": "BTC", "tf": "", "drawings": [], "updatedAt": 0})
        r = self.c.put("/api/v1/chart/drawings", json={"symbol": "BTC", "drawings": [seg()], "baseUpdatedAt": 0})
        self.assertEqual(r.status_code, 200, r.text)
        t1 = r.json()["data"]["updatedAt"]
        self.assertGreater(t1, 0)
        got = self.c.get("/api/v1/chart/drawings?symbol=BTC").json()["data"]
        self.assertEqual(len(got["drawings"]), 1)
        # stale base -> 409 with the current document
        r = self.c.put("/api/v1/chart/drawings", json={"symbol": "BTC", "drawings": [], "baseUpdatedAt": 0})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["error"]["current"]["updatedAt"], t1)
        # validation
        for body in (
            {"symbol": "BTC", "drawings": [{"name": "x", "points": []}]},
            {"symbol": "../etc", "drawings": []},
            {"symbol": "BTC", "tf": "7h", "drawings": []},
            {"symbol": "BTC", "drawings": "no"},
            {"symbol": "BTC", "drawings": [], "baseUpdatedAt": "1"},
        ):
            self.assertEqual(self.c.put("/api/v1/chart/drawings", json=body).status_code, 400, body)
        self.assertEqual(self.c.put("/api/v1/chart/drawings", content=b"{nope", headers={"content-type": "application/json"}).status_code, 400)
        self.assertEqual(self.c.put("/api/v1/chart/drawings", content=b"x" * 70_000, headers={"content-type": "application/json"}).status_code, 413)
        idx = self.c.get("/api/v1/chart/drawings/index").json()["data"]["charts"]
        self.assertEqual([(c["symbol"], c["count"]) for c in idx], [("BTC", 1)])

    def test_users_are_isolated(self):
        self._login("alice")
        self.c.put("/api/v1/chart/drawings", json={"symbol": "BTC", "drawings": [seg()]})
        self.c.post("/api/v1/auth/logout")
        self._login("bob")
        self.assertEqual(self.c.get("/api/v1/chart/drawings?symbol=BTC").json()["data"]["drawings"], [])

    def test_write_rate_limit(self):
        self._login()
        codes = [self.c.put("/api/v1/chart/drawings", json={"symbol": "BTC", "drawings": [seg(i)]}).status_code for i in range(cd.WRITE_LIMIT.limit + 3)]
        self.assertEqual(codes.count(200), cd.WRITE_LIMIT.limit)
        self.assertEqual(codes[-1], 429)

    def test_standby_refuses_writes_but_reads(self):
        self._login()
        os.environ["AUU_ROLE"] = "standby"
        r = self.c.put("/api/v1/chart/drawings", json={"symbol": "BTC", "drawings": [seg()]})
        self.assertEqual(r.status_code, 503)
        self.assertEqual(self.c.get("/api/v1/chart/drawings?symbol=BTC").status_code, 200)


if __name__ == "__main__":
    unittest.main()
