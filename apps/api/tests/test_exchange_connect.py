"""Exchange connect (Binance/OKX spot, per-user keys): ccxt fully mocked, no network."""
from __future__ import annotations

import base64
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.auth.accounts import reset_accounts
from app.exchange import connector, store
from app.exchange import service as svc

PW = "exchPass1234"
KEYS = ("AUU_AUTH", "AUU_ALLOW_SIGNUP", "AUU_INVITE_CODE", "AUU_ADMIN_USER", "AUU_USER_STORE", "AUU_USER_JOURNAL_DIR",
        "AUU_AUTH_RATE_MAX", "AUU_EXCHANGE_STORE", "AUU_EXCHANGE_MASTER_KEY", "DATA_PROVIDER", "AUU_ROLE")
SECRET = "SUPERSECRETvalue123456"


class FakeClient:
    instances: list["FakeClient"] = []
    restrictions = {"enableReading": True, "enableSpotAndMarginTrading": True, "enableWithdrawals": False, "ipRestrict": True}
    okx_perm = {"perm": "read_only,trade", "ip": "156.227.238.242"}
    create_exc: Exception | None = None

    def __init__(self, exchange, creds):
        self.exchange, self.creds = exchange, creds
        self.options = {}
        self.created: list = []
        self.cancelled: list = []
        FakeClient.instances.append(self)

    def set_markets(self, m, c):
        pass

    def sapiGetAccountApiRestrictions(self):
        return dict(FakeClient.restrictions)

    def privateGetAccountConfig(self):
        return {"data": [dict(FakeClient.okx_perm)]}

    def fetch_balance(self):
        return {"total": {"USDT": 100.0, "BTC": 0.0}, "free": {"USDT": 90.0}, "used": {"USDT": 10.0}}

    def fetch_open_orders(self, symbol=None):
        return [{"id": "1", "symbol": "BTC/USDT", "side": "buy", "type": "limit", "price": 10, "amount": 1, "status": "open"}]

    def fetch_closed_orders(self, symbol=None, since=None, limit=None):
        return [{"id": "2", "symbol": symbol, "status": "closed", "timestamp": 1}]

    def fetch_ticker(self, symbol):
        return {"ask": 100.0, "bid": 99.0, "last": 99.5}

    def create_order(self, symbol, otype, side, amount, price, params):
        if FakeClient.create_exc is not None:
            raise FakeClient.create_exc
        self.created.append((symbol, otype, side, amount, price, params))
        return {"id": "o" + str(len(self.created)), "clientOrderId": params["clientOrderId"], "symbol": symbol, "status": "open"}

    def fetch_order(self, oid, symbol, params):
        return {"id": "found", "clientOrderId": params["clientOrderId"], "symbol": symbol, "status": "open"}

    def cancel_order(self, oid, symbol):
        self.cancelled.append((oid, symbol))
        return {"id": oid, "symbol": symbol, "status": "canceled"}


class RequestTimeout(Exception):
    pass


def fake_make(exchange, creds=None):
    return FakeClient(exchange, creds)


class ExchangeApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.prev = {k: os.environ.get(k) for k in KEYS}
        os.environ.update({
            "AUU_AUTH": "on", "AUU_ALLOW_SIGNUP": "on", "AUU_USER_STORE": str(root / "users.json"),
            "AUU_USER_JOURNAL_DIR": str(root / "j"), "AUU_AUTH_RATE_MAX": "1000", "DATA_PROVIDER": "mock",
            "AUU_EXCHANGE_STORE": str(root / "exchange.sqlite"),
            "AUU_EXCHANGE_MASTER_KEY": base64.urlsafe_b64encode(os.urandom(32)).decode(),
        })
        for k in ("AUU_INVITE_CODE", "AUU_ADMIN_USER", "AUU_ROLE"):
            os.environ.pop(k, None)
        reset_accounts(); store.reset(); svc.reset_rate()
        FakeClient.instances = []; FakeClient.create_exc = None
        FakeClient.restrictions = {"enableReading": True, "enableSpotAndMarginTrading": True, "enableWithdrawals": False, "ipRestrict": True}
        FakeClient.okx_perm = {"perm": "read_only,trade", "ip": "156.227.238.242"}
        self.p1 = mock.patch.object(connector, "make_client", side_effect=fake_make)
        self.p1.start()
        connector._MARKETS.clear()
        from fastapi.testclient import TestClient
        from app.main import app
        self.TC, self.app = TestClient, app
        self.admin = self._reg("exadmin")  # first user is admin
        self.user = self._reg("exuser")

    def tearDown(self):
        self.p1.stop()
        for k, v in self.prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        reset_accounts(); store.reset(); self.tmp.cleanup()

    def _reg(self, name):
        c = self.TC(self.app)
        r = c.post("/api/v1/auth/register", json={"name": name, "password": PW, "password_confirm": PW})
        self.assertEqual(r.status_code, 200, r.text)
        return c

    def _open_for_user(self):
        self.assertEqual(self.admin.put("/api/v1/admin/exchange", json={"enabled": True}).status_code, 200)
        r = self.admin.post("/api/v1/admin/exchange/allowlist", json={"user": "exuser", "allow": True})
        self.assertEqual(r.status_code, 200, r.text)

    def _bind(self, ex="binance", **kw):
        body = {"apiKey": "AKEYabcdefgh1234", "secret": SECRET, **kw}
        return self.user.post(f"/api/v1/exchange/{ex}/bind", json=body)

    def test_unauthenticated_401(self):
        anon = self.TC(self.app)
        for m, p in [("GET", "/api/v1/exchange/config"), ("GET", "/api/v1/exchange/accounts"),
                     ("GET", "/api/v1/exchange/binance/balances"), ("POST", "/api/v1/exchange/binance/orders"),
                     ("POST", "/api/v1/exchange/binance/bind"), ("DELETE", "/api/v1/exchange/binance/bind"),
                     ("GET", "/api/v1/admin/exchange"), ("PUT", "/api/v1/admin/exchange")]:
            with self.subTest(p=p, m=m):
                self.assertEqual(anon.request(m, p, json={}).status_code, 401)

    def test_default_off_and_live_untouched(self):
        cfg = self.user.get("/api/v1/exchange/config").json()["data"]
        self.assertFalse(cfg["enabled"]); self.assertFalse(cfg["access"])
        self.assertEqual(cfg["serverIps"]["primary"], "156.227.238.242")
        self.assertEqual(self._bind().status_code, 403)
        self.assertEqual(self.user.get("/api/v1/admin/exchange").status_code, 403)
        self.assertEqual(self.user.put("/api/v1/admin/exchange", json={"enabled": True}).status_code, 403)
        self.assertFalse(self.admin.get("/api/v1/health").json()["data"]["liveEnabled"])

    def test_allowlist_required(self):
        self.admin.put("/api/v1/admin/exchange", json={"enabled": True})
        self.assertEqual(self._bind().json()["error"]["code"], "EXCHANGE_NOT_ALLOWED")

    def test_bind_rejects_withdraw_and_no_ip(self):
        self._open_for_user()
        FakeClient.restrictions = {**FakeClient.restrictions, "enableWithdrawals": True}
        self.assertEqual(self._bind().json()["error"]["code"], "KEY_HAS_WITHDRAW")
        FakeClient.restrictions = {**FakeClient.restrictions, "enableWithdrawals": False, "ipRestrict": False}
        self.assertEqual(self._bind().json()["error"]["code"], "KEY_NO_IP_WHITELIST")
        FakeClient.okx_perm = {"perm": "read_only,trade,withdraw", "ip": "1.2.3.4"}
        self.assertEqual(self._bind("okx", passphrase="pp").json()["error"]["code"], "KEY_HAS_WITHDRAW")
        self.assertEqual(self._bind("okx").json()["error"]["code"], "BAD_KEY")  # okx needs passphrase
        self.assertEqual(self.user.get("/api/v1/exchange/accounts").json()["data"]["items"], [])

    def test_bind_encrypted_masked_unbind(self):
        self._open_for_user()
        r = self._bind()
        self.assertEqual(r.status_code, 200, r.text)
        self.assertNotIn(SECRET, r.text)
        self.assertIn("…", r.json()["data"]["masked"])
        raw = Path(os.environ["AUU_EXCHANGE_STORE"]).read_bytes()
        self.assertNotIn(SECRET.encode(), raw)
        self.assertNotIn(b"AKEYabcdefgh1234", raw)
        items = self.user.get("/api/v1/exchange/accounts").json()["data"]["items"]
        self.assertEqual(len(items), 1); self.assertNotIn(SECRET, json.dumps(items))
        audit = self.admin.get("/api/v1/admin/exchange/audit").text
        self.assertNotIn(SECRET, audit); self.assertIn("bind", audit)
        bal = self.user.get("/api/v1/exchange/binance/balances").json()["data"]["items"]
        self.assertEqual(bal, [{"asset": "USDT", "total": 100.0, "free": 90.0, "used": 10.0}])
        self.assertEqual([c for c in FakeClient.instances if c.creds][-1].creds["secret"], SECRET)  # decrypted correctly
        self.assertTrue(self.user.delete("/api/v1/exchange/binance/bind").json()["data"]["deleted"])
        con = sqlite3.connect(os.environ["AUU_EXCHANGE_STORE"])
        self.assertEqual(con.execute("SELECT COUNT(*) FROM creds").fetchone()[0], 0)
        self.assertEqual(self.user.get("/api/v1/exchange/binance/balances").json()["error"]["code"], "NOT_BOUND")

    def test_wrong_master_key_cannot_decrypt(self):
        self._open_for_user(); self._bind()
        os.environ["AUU_EXCHANGE_MASTER_KEY"] = base64.urlsafe_b64encode(os.urandom(32)).decode()
        self.assertEqual(self.user.get("/api/v1/exchange/binance/balances").json()["error"]["code"], "CRED_UNREADABLE")

    def test_orders_cap_idempotency_kill_cancel(self):
        self._open_for_user(); self._bind()
        base = {"clientOrderId": "abcd1234efgh", "symbol": "BTC/USDT", "side": "buy", "type": "limit", "amount": 0.1, "price": 100}
        r = self.user.post("/api/v1/exchange/binance/orders", json=base)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["data"]["order"]["clientOrderId"], "auuabcd1234efgh")
        r2 = self.user.post("/api/v1/exchange/binance/orders", json=base)
        self.assertTrue(r2.json()["data"]["duplicate"])
        self.assertEqual(sum(len(c.created) for c in FakeClient.instances), 1)  # no resend
        big = {**base, "clientOrderId": "bigorder0001", "amount": 1}  # 100 USDT > 50 cap
        self.assertEqual(self.user.post("/api/v1/exchange/binance/orders", json=big).json()["error"]["code"], "ORDER_TOO_LARGE")
        mk = {**base, "clientOrderId": "market000001", "type": "market", "amount": 0.4, "price": None}  # 0.4*100 ask = 40
        self.assertEqual(self.user.post("/api/v1/exchange/binance/orders", json=mk).status_code, 200)
        self.assertEqual(self.user.post("/api/v1/exchange/binance/orders", json={**mk, "clientOrderId": "bad"}).json()["error"]["code"], "BAD_CLIENT_ORDER_ID")
        self.assertEqual(self.user.post("/api/v1/exchange/binance/orders", json={**mk, "clientOrderId": "eth00000001x", "symbol": "ETH/BTC"}).json()["error"]["code"], "BAD_SYMBOL")
        self.admin.put("/api/v1/admin/exchange", json={"killSwitch": True})
        k = self.user.post("/api/v1/exchange/binance/orders", json={**base, "clientOrderId": "killed000001"})
        self.assertEqual(k.json()["error"]["code"], "KILL_SWITCH")
        c = self.user.post("/api/v1/exchange/binance/orders/cancel", json={"orderId": "o1", "symbol": "BTC/USDT"})
        self.assertEqual(c.json()["data"]["status"], "canceled")
        self.assertEqual(len(self.user.get("/api/v1/exchange/binance/orders/open").json()["data"]["items"]), 1)
        self.assertEqual(self.user.get("/api/v1/exchange/binance/orders/history").json()["error"]["code"], "BAD_SYMBOL")
        self.assertEqual(self.user.get("/api/v1/exchange/binance/orders/history?symbol=BTC").status_code, 200)

    def test_per_user_cap_override(self):
        self._open_for_user()
        self.admin.post("/api/v1/admin/exchange/allowlist", json={"user": "exuser", "allow": True, "capUsdt": 200})
        self._bind()
        r = self.user.post("/api/v1/exchange/binance/orders", json={"clientOrderId": "capover00001", "symbol": "BTC", "side": "buy", "type": "limit", "amount": 1, "price": 150})
        self.assertEqual(r.status_code, 200, r.text)

    def test_unknown_order_reconciled_not_resent(self):
        self._open_for_user(); self._bind()
        FakeClient.create_exc = RequestTimeout("t")
        body = {"clientOrderId": "timeout00001", "symbol": "BTC/USDT", "side": "buy", "type": "limit", "amount": 0.1, "price": 100}
        self.assertEqual(self.user.post("/api/v1/exchange/binance/orders", json=body).json()["error"]["code"], "ORDER_UNKNOWN")
        FakeClient.create_exc = None
        r = self.user.post("/api/v1/exchange/binance/orders", json=body).json()["data"]
        self.assertEqual(r["order"]["id"], "found"); self.assertTrue(r["duplicate"])
        self.assertEqual(sum(len(c.created) for c in FakeClient.instances), 0)

    def test_rate_limit(self):
        self._open_for_user(); self._bind()
        codes = [self.user.get("/api/v1/exchange/accounts").status_code for _ in range(svc.READ_LIMIT + 2)]
        self.assertIn(429, codes)

    def test_standby_blocks_writes(self):
        self._open_for_user()
        os.environ["AUU_ROLE"] = "standby"
        self.assertEqual(self._bind().status_code, 503)


if __name__ == "__main__":
    unittest.main()
