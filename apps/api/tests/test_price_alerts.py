"""Price alerts: validation, fire-once crossing logic, caps, mail (fake sender), routes, standby."""
from __future__ import annotations

import os
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import price_alerts as pa

T0 = 1_760_000_000_000


def mk(**prices):
    """markets()-shaped payload with fresh ticker prices."""
    return {"exchange": "binance", "items": [{"symbol": s, "price": p, "priceTs": T0} for s, p in prices.items()]}


class ValidateTests(unittest.TestCase):
    def test_fields(self):
        self.assertEqual(pa.normalize_symbol("btc/usdt"), "BTC")
        self.assertEqual(pa.normalize_direction(" ABOVE "), "above")
        self.assertEqual(pa.normalize_price(65000.123456789012), 65000.12346)
        self.assertEqual(pa.normalize_note("a\nb\x00c" + "x" * 200), "a b c" + "x" * 75)
        for bad in (None, "1", True, 0, -1, float("nan"), float("inf"), 1e10):
            with self.assertRaises(pa.AlertError, msg=repr(bad)):
                pa.normalize_price(bad)
        for fn, bad in ((pa.normalize_symbol, "../x"), (pa.normalize_symbol, ""), (pa.normalize_direction, "up"),
                        (pa.normalize_venue, "ftx"), (pa.normalize_note, {"a": 1})):
            with self.assertRaises(pa.AlertError):
                fn(bad)

    def test_fresh_prices_ignores_stale_and_empty(self):
        m = {"items": [{"symbol": "BTC", "price": 1.0, "priceTs": T0}, {"symbol": "ETH", "price": 2.0, "priceTs": T0 - pa.MAX_PRICE_AGE_MS - 1},
                       {"symbol": "SOL", "price": None, "priceTs": T0}, {"symbol": "XRP", "price": 3.0, "priceTs": None}]}
        self.assertEqual(pa.fresh_prices(m, T0), {"BTC": 1.0})


class StoreBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = pa.PriceAlertStore(Path(self.tmp.name) / "price_alerts.sqlite")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()


class CrossingTests(StoreBase):
    def test_above_fires_once_on_upcross_only(self):
        a = self.store.create("u1", "BTC", 100.0, "above", side=-1, now_ms=T0)
        self.assertEqual(self.store.evaluate({"BTC": 99.0}, now_ms=T0), [])
        fired = self.store.evaluate({"BTC": 100.0}, now_ms=T0 + 1)
        self.assertEqual([(f["alertId"], f["firedDir"], f["price"]) for f in fired], [(a["id"], "up", 100.0)])
        # price wobbles around the target afterwards: never fires again
        for p in (99.0, 101.0, 99.5, 100.5):
            self.assertEqual(self.store.evaluate({"BTC": p}, now_ms=T0 + 2), [])
        row = self.store.list("u1")[0]
        self.assertEqual((row["status"], row["triggerPrice"], row["triggeredAt"]), ("triggered", 100.0, T0 + 1))
        self.assertEqual(len(self.store.events("u1")), 1)

    def test_above_ignores_downcross_but_tracks_side(self):
        self.store.create("u1", "BTC", 100.0, "above", side=1, now_ms=T0)  # created while already above (side known)
        self.assertEqual(self.store.evaluate({"BTC": 90.0}, now_ms=T0), [])  # fell below: not its direction
        self.assertEqual(len(self.store.evaluate({"BTC": 101.0}, now_ms=T0)), 1)  # came back up: fires

    def test_below_and_cross(self):
        self.store.create("u1", "ETH", 50.0, "below", side=1, now_ms=T0)
        c = self.store.create("u1", "ETH", 60.0, "cross", side=-1, now_ms=T0)
        self.assertEqual(self.store.evaluate({"ETH": 55.0}, now_ms=T0), [])
        f = self.store.evaluate({"ETH": 49.0}, now_ms=T0)
        self.assertEqual([(x["direction"], x["firedDir"]) for x in f], [("below", "down")])
        f = self.store.evaluate({"ETH": 61.0}, now_ms=T0)
        self.assertEqual([(x["alertId"], x["firedDir"]) for x in f], [(c["id"], "up")])

    def test_first_price_only_records_side(self):
        self.store.create("u1", "SOL", 10.0, "cross", now_ms=T0)  # no price at creation
        self.assertEqual(self.store.evaluate({"SOL": 12.0}, now_ms=T0), [])  # first sight: remember, do not fire
        self.assertEqual(len(self.store.evaluate({"SOL": 9.0}, now_ms=T0)), 1)

    def test_missing_symbol_price_keeps_alert_active(self):
        self.store.create("u1", "BTC", 100.0, "above", side=-1, now_ms=T0)
        self.assertEqual(self.store.evaluate({"ETH": 5.0}, now_ms=T0), [])
        self.assertEqual(self.store.list("u1", status="active")[0]["status"], "active")

    def test_two_checkers_cannot_double_fire(self):
        self.store.create("u1", "BTC", 100.0, "above", side=-1, now_ms=T0)
        other = pa.PriceAlertStore(self.store.path)  # second process / second checker on the same DB
        try:
            n = len(self.store.evaluate({"BTC": 101.0}, now_ms=T0)) + len(other.evaluate({"BTC": 101.0}, now_ms=T0))
        finally:
            other.close()
        self.assertEqual(n, 1)
        self.assertEqual(len(self.store.events("u1")), 1)

    def test_deleted_alert_does_not_fire(self):
        a = self.store.create("u1", "BTC", 100.0, "above", side=-1, now_ms=T0)
        self.assertTrue(self.store.delete("u1", a["id"]))
        self.assertEqual(self.store.evaluate({"BTC": 101.0}, now_ms=T0), [])


class CapTests(StoreBase):
    def test_cap_duplicates_isolation_and_history(self):
        for i in range(pa.MAX_ACTIVE):
            self.store.create("u1", "BTC", 100.0 + i, "above", now_ms=T0)
        with self.assertRaises(pa.AlertError) as cm:
            self.store.create("u1", "BTC", 999.0, "above", now_ms=T0)
        self.assertEqual((cm.exception.code, cm.exception.status), ("ALERT_LIMIT", 409))
        with self.assertRaises(pa.AlertError) as cm:
            self.store.create("u1", "BTC", 100.0, "above", now_ms=T0, max_active=99)
        self.assertEqual(cm.exception.code, "ALERT_DUPLICATE")
        self.store.create("u2", "BTC", 100.0, "above", now_ms=T0)  # another user is not affected
        self.assertEqual(self.store.list("u2")[0]["price"], 100.0)
        self.assertEqual(self.store.list("u2", symbol="ETH"), [])
        # a triggered alert frees a slot, and only the owner can delete / read
        self.store.evaluate({"BTC": 1.0}, now_ms=T0)  # records sides
        self.store.evaluate({"BTC": 1e6}, now_ms=T0 + 1)
        self.assertEqual(self.store.counts("u1").get("active", 0), 0)
        self.assertFalse(self.store.delete("u2", self.store.list("u1")[0]["id"]))
        self.assertEqual(self.store.clear_history("u1"), pa.MAX_ACTIVE)
        self.assertEqual(self.store.list("u1"), [])


class NotificationTests(StoreBase):
    def test_unread_and_mark_read(self):
        self.store.create("u1", "BTC", 100.0, "above", side=-1, now_ms=T0)
        self.store.create("u1", "BTC", 120.0, "above", side=-1, now_ms=T0)
        self.store.evaluate({"BTC": 130.0}, now_ms=T0 + 5, venue="okx")
        self.assertEqual(self.store.unread("u1"), 2)
        self.assertEqual(self.store.unread("u2"), 0)
        ev = self.store.events("u1")
        self.assertEqual({e["venue"] for e in ev}, {"okx"})
        self.assertEqual(self.store.mark_read("u2", [ev[0]["id"]]), 0)  # not theirs
        self.assertEqual(self.store.mark_read("u1", [ev[0]["id"]]), 1)
        self.assertEqual(self.store.unread("u1"), 1)
        self.assertEqual(self.store.mark_read("u1"), 1)
        self.assertEqual(self.store.events("u1", unread_only=True), [])


class MailTests(StoreBase):
    def checker(self, users, sent, *, fail=0, configured=True):
        box = {"now": T0 + 10}
        calls = {"n": 0}

        def send(to, subject, body, html_body):
            calls["n"] += 1
            if calls["n"] <= fail:
                raise OSError("smtp down alice@example.com")
            sent.append((to, subject, body, html_body))

        c = pa.AlertChecker(self.store, markets_fn=lambda: mk(BTC=130.0), user_fn=users.get, send=send,
                            configured=lambda: configured, now_ms=lambda: box["now"])
        c.box = box
        return c

    def fire(self, uid="u1", n=1):
        for i in range(n):
            self.store.create(uid, "BTC", 50.0 + i, "above", side=-1, note="my level", now_ms=T0, max_active=999)
        self.store.evaluate({"BTC": 130.0}, now_ms=T0 + 1, venue="binance")

    def test_mail_in_user_language_once(self):
        self.fire()
        sent: list = []
        c = self.checker({"u1": {"email": "vin@example.com", "locale": "de"}}, sent)
        self.assertEqual(c.deliver(), 1)
        self.assertEqual(c.deliver(), 0)  # never twice
        to, subject, body, html_body = sent[0]
        self.assertEqual(to, "vin@example.com")
        self.assertIn("AUUTRADE Preisalarm: BTC", subject)
        self.assertIn("my level", body)
        self.assertIn("AUUTRADE", body.splitlines()[-3] + body)  # signature
        self.assertIn("<html", html_body)
        self.assertEqual(self.store.events("u1")[0]["email"], "sent")

    def test_no_email_unconfigured_and_unknown_user(self):
        self.fire("u1")
        self.fire("u2")
        self.fire("u3")
        sent: list = []
        c = self.checker({"u1": {"email": ""}, "u2": {"email": "b@example.com"}}, sent, configured=False)
        self.assertEqual(c.deliver(), 0)
        st = {e["email"] for u in ("u1", "u2", "u3") for e in self.store.events(u)}
        self.assertEqual(st, {"no_email", "unconfigured"})
        self.assertEqual(sent, [])

    def test_retry_with_backoff_then_sent(self):
        self.fire()
        sent: list = []
        c = self.checker({"u1": {"email": "vin@example.com"}}, sent, fail=2)
        self.assertEqual(c.deliver(), 0)
        self.assertEqual(self.store.events("u1")[0]["email"], "failed")
        self.assertEqual(c.deliver(), 0)  # still inside the backoff window
        c.box["now"] += 3 * 60_000
        self.assertEqual(c.deliver(), 0)  # second failure
        c.box["now"] += 11 * 60_000
        self.assertEqual(c.deliver(), 1)
        self.assertEqual(len(sent), 1)
        c.box["now"] += pa.MAIL_QUEUE_MS
        self.assertEqual(c.deliver(), 0)

    def test_mail_flood_guard(self):
        self.fire("u1", n=pa.MAIL_PER_USER_HOUR + 3)
        sent: list = []
        c = self.checker({"u1": {"email": "vin@example.com"}}, sent)
        total = 0
        for _ in range(10):
            total += c.deliver()
        self.assertEqual(total, pa.MAIL_PER_USER_HOUR)
        ev = self.store.events("u1", limit=100)
        self.assertEqual(sum(e["email"] == "suppressed" for e in ev), 3)  # still in-site notifications
        self.assertEqual(len(ev), pa.MAIL_PER_USER_HOUR + 3)

    def test_render_all_languages(self):
        from app.auth.email_i18n import EMAIL_LANGS

        ev = {"symbol": "BTC", "target": 65000.5, "triggerPrice": 65010.0, "firedDir": "up", "createdAt": T0, "venue": "okx", "note": "<b>x</b>"}
        for lang in EMAIL_LANGS:
            for d in ("up", "down"):
                subject, text, html_body = pa.render_mail(lang, {**ev, "firedDir": d})
                self.assertIn("AUUTRADE", subject)
                self.assertIn("65,000.5", subject)
                self.assertIn("&lt;b&gt;x&lt;/b&gt;", html_body)  # note is escaped
                self.assertNotIn("<b>x</b>", html_body)
                self.assertIn("OKX", text)
        self.assertEqual(set(pa.MAIL_TEXTS), set(EMAIL_LANGS))
        self.assertEqual(pa.render_mail("xx", ev)[0], pa.render_mail("zh-CN", ev)[0])  # unknown language -> default

    def test_tick_skips_market_service_without_alerts(self):
        called = []
        c = pa.AlertChecker(self.store, markets_fn=lambda: called.append(1) or mk(BTC=1.0), user_fn=lambda u: None, send=lambda *a: None, now_ms=lambda: T0)
        self.assertEqual(c.tick(), {"fired": 0, "mailed": 0})
        self.assertEqual(called, [])
        self.store.create("u1", "BTC", 100.0, "above", now_ms=T0)
        c.tick()
        self.assertEqual(called, [1])

    def test_stale_prices_do_not_fire(self):
        self.store.create("u1", "BTC", 100.0, "above", side=-1, now_ms=T0)
        c = pa.AlertChecker(self.store, markets_fn=lambda: {"exchange": "binance", "items": [{"symbol": "BTC", "price": 500.0, "priceTs": T0}]},
                            user_fn=lambda u: None, send=lambda *a: None, now_ms=lambda: T0 + pa.MAX_PRICE_AGE_MS + 1)
        self.assertEqual(c.check(), [])
        self.assertEqual(self.store.list("u1")[0]["status"], "active")


class RouteTests(unittest.TestCase):
    _ENV = ("AUU_AUTH", "AUU_ALLOW_SIGNUP", "AUU_USER_STORE", "AUU_USER_JOURNAL_DIR", "AUU_AUTH_RATE_MAX", "AUU_LEGACY_PUMP", "AUU_INVITE_CODE", "AUU_ADMIN_USER", "AUU_ROLE")

    def setUp(self):
        from app.auth.accounts import reset_accounts

        self.tmp = tempfile.TemporaryDirectory()
        self._prev = {k: os.environ.get(k) for k in self._ENV}
        root = Path(self.tmp.name)
        os.environ.update({"AUU_AUTH": "on", "AUU_ALLOW_SIGNUP": "on", "AUU_USER_STORE": str(root / "users.json"),
                           "AUU_USER_JOURNAL_DIR": str(root / "journals"), "AUU_AUTH_RATE_MAX": "1000", "AUU_LEGACY_PUMP": "off"})
        for k in ("AUU_INVITE_CODE", "AUU_ADMIN_USER", "AUU_ROLE"):
            os.environ.pop(k, None)
        reset_accounts()
        self.store = pa.PriceAlertStore(root / "price_alerts.sqlite")
        pa.reset_store(self.store)
        from app.routes import price_alerts as routes

        self.routes = routes
        routes.WRITE_LIMIT.reset()
        routes.READ_LIMIT.reset()
        self.price = {"BTC": 100.0}
        from types import SimpleNamespace

        import time

        svc = SimpleNamespace(cfg=SimpleNamespace(all_symbols=lambda: ["BTC", "ETH", "SOL"]))
        self._mk = patch.object(routes, "_market", lambda: (svc, {"exchange": "okx", "items": [
            {"symbol": s, "price": p, "priceTs": int(time.time() * 1000)} for s, p in self.price.items()]}))
        self._mk.start()
        self._sock = patch.object(socket.socket, "connect", side_effect=AssertionError("network access in test"))
        self._sock.start()
        from fastapi.testclient import TestClient

        from app.main import create_app

        self.c = TestClient(create_app(legacy=False))

    def tearDown(self):
        from app.auth.accounts import reset_accounts

        self._sock.stop()
        self._mk.stop()
        pa.reset_store(None)
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

    def post(self, **body):
        return self.c.post("/api/v1/chart/alerts", json={"symbol": "BTC", "direction": "above", "price": 110, **body})

    def test_login_required_everywhere(self):
        for method, url in (("get", "/api/v1/chart/alerts"), ("post", "/api/v1/chart/alerts"), ("delete", "/api/v1/chart/alerts/1"),
                            ("delete", "/api/v1/chart/alerts?scope=history"), ("get", "/api/v1/chart/alerts/notifications"),
                            ("post", "/api/v1/chart/alerts/notifications/read")):
            self.assertEqual(getattr(self.c, method)(url).status_code, 401, url)

    def test_create_list_delete_and_validation(self):
        self._login()
        r = self.post(note="breakout")
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()["data"]
        self.assertEqual((d["alert"]["symbol"], d["alert"]["venue"], d["alert"]["status"], d["last"]), ("BTC", "okx", "active", 100.0))
        aid = d["alert"]["id"]
        lst = self.c.get("/api/v1/chart/alerts?symbol=btc").json()["data"]
        self.assertEqual([a["id"] for a in lst["alerts"]], [aid])
        self.assertEqual((lst["caps"]["active"], lst["caps"]["maxActive"], lst["unread"], lst["emailOn"]), (1, 20, 0, False))
        self.assertEqual(self.post().status_code, 409)  # exact duplicate
        bad = [{"price": "x"}, {"price": -1}, {"symbol": "../x"}, {"symbol": "DOGEX"}, {"direction": "sideways"}, {"note": 5}, {"venue": "ftx"},
               {"price": 90}]  # 90 "above" while price is 100: already reached
        for b in bad:
            r = self.post(**b)
            self.assertEqual(r.status_code, 400, (b, r.text))
        self.assertEqual(self.post(price=90, direction="below").status_code, 200)
        self.assertEqual(self.post(price=90, direction="cross").status_code, 200)
        self.assertEqual(self.c.post("/api/v1/chart/alerts", content=b"{nope", headers={"content-type": "application/json"}).status_code, 400)
        self.assertEqual(self.c.post("/api/v1/chart/alerts", content=b"x" * 3000, headers={"content-type": "application/json"}).status_code, 413)
        self.assertEqual(self.c.delete(f"/api/v1/chart/alerts/{aid}").status_code, 200)
        self.assertEqual(self.c.delete(f"/api/v1/chart/alerts/{aid}").status_code, 404)
        self.assertEqual(self.c.get("/api/v1/chart/alerts?status=bogus").status_code, 400)
        self.assertEqual(self.c.delete("/api/v1/chart/alerts").status_code, 400)

    def test_cap_returns_409(self):
        self._login()
        for i in range(pa.MAX_ACTIVE):
            self.assertEqual(self.post(price=200 + i).status_code, 200)
        self.routes.WRITE_LIMIT.reset()
        r = self.post(price=999)
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "ALERT_LIMIT"))

    def test_users_are_isolated_and_fire_flow(self):
        self._login("alice")
        aid = self.post().json()["data"]["alert"]["id"]
        self.c.post("/api/v1/auth/logout")
        self._login("bob")
        self.assertEqual(self.c.get("/api/v1/chart/alerts").json()["data"]["alerts"], [])
        self.assertEqual(self.c.delete(f"/api/v1/chart/alerts/{aid}").status_code, 404)
        self.c.post("/api/v1/auth/logout")
        self.c.post("/api/v1/auth/login", json={"name": "alice", "password": "paperPass123"})
        # the checker fires it; alice sees badge + notification, marks read
        self.assertEqual(len(self.store.evaluate({"BTC": 111.0}, now_ms=T0, venue="okx")), 1)
        d = self.c.get("/api/v1/chart/alerts").json()["data"]
        self.assertEqual((d["unread"], d["alerts"][0]["status"], d["alerts"][0]["triggerPrice"]), (1, "triggered", 111.0))
        n = self.c.get("/api/v1/chart/alerts/notifications?unread=1").json()["data"]
        self.assertEqual((n["unread"], n["events"][0]["alertId"], n["events"][0]["firedDir"]), (1, aid, "up"))
        self.assertEqual(self.c.post("/api/v1/chart/alerts/notifications/read", json={"ids": ["1"]}).status_code, 400)
        self.assertEqual(self.c.post("/api/v1/chart/alerts/notifications/read", json={}).json()["data"], {"marked": 1, "unread": 0})
        self.assertEqual(self.c.delete("/api/v1/chart/alerts?scope=history").json()["data"]["cleared"], 1)

    def test_write_rate_limit(self):
        self._login()
        codes = [self.post(price=200 + i).status_code for i in range(self.routes.WRITE_LIMIT.limit + 3)]
        self.assertEqual(codes[-1], 429)
        self.assertEqual(codes.count(200), self.routes.WRITE_LIMIT.limit)

    def test_standby_refuses_everything(self):
        self._login()
        os.environ["AUU_ROLE"] = "standby"
        self.assertFalse(pa.price_alerts_enabled())
        for r in (self.post(), self.c.get("/api/v1/chart/alerts"), self.c.get("/api/v1/chart/alerts/notifications"), self.c.delete("/api/v1/chart/alerts/1")):
            self.assertEqual(r.status_code, 503)
        self.assertEqual(self.store.list("local") + self.store.list("x"), [])


if __name__ == "__main__":
    unittest.main()
