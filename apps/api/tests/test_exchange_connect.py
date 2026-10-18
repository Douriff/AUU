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
    restrictions = {"enableReading": True, "enableSpotAndMarginTrading": True, "enableFutures": True, "enableWithdrawals": False, "ipRestrict": True}
    okx_perm = {"perm": "read_only,trade", "ip": "156.227.238.242"}
    create_exc: Exception | None = None

    positions = []

    def __init__(self, exchange, creds, market="spot"):
        self.exchange, self.creds, self.market_type = exchange, creds, market
        self.leverage = []
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

    def market(self, sym):
        return {"contractSize": 0.01 if self.exchange == "okx" else 1}

    def fetch_positions(self, symbols=None):
        return list(FakeClient.positions)

    def fetch_funding_rate(self, sym):
        return {"fundingRate": 0.0001, "fundingTimestamp": 1, "markPrice": 100.0}

    def set_margin_mode(self, mode, sym, params=None):
        self.leverage.append(("mode", mode, sym, params))

    def set_leverage(self, lev, sym, params=None):
        self.leverage.append(("lev", lev, sym, params))

    def cancel_all_orders(self, sym):
        self.cancelled.append(("all", sym))

    def cancel_order(self, oid, symbol):
        self.cancelled.append((oid, symbol))
        return {"id": oid, "symbol": symbol, "status": "canceled"}


def _confirming(c):
    """The web UI sends `confirmed: true` after its confirmation dialog and the server now insists on it.
    These older tests exercise other rules, so their client adds the flag (new tests use raw clients)."""
    raw_post = c.post

    def post(url, *a, **kw):
        j = kw.get("json")
        if isinstance(j, dict) and url.endswith(("/orders", "/futures/orders")) and "confirmed" not in j:
            kw["json"] = {**j, "confirmed": True}
        return raw_post(url, *a, **kw)

    c.post = post
    return c


class RequestTimeout(Exception):
    pass


def fake_make(exchange, creds=None, market="spot"):
    return FakeClient(exchange, creds, market)


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
        FakeClient.instances = []; FakeClient.create_exc = None; FakeClient.positions = []
        FakeClient.restrictions = {"enableReading": True, "enableSpotAndMarginTrading": True, "enableFutures": True, "enableWithdrawals": False, "ipRestrict": True}
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
        return _confirming(c)

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

    def _creates(self):
        return [x for c in FakeClient.instances for x in c.created]

    def test_futures_ack_leverage_cap_tpsl_kill(self):
        self._open_for_user(); self._bind()
        sym = "BTC/USDT:USDT"
        self.assertEqual(self.user.get("/api/v1/exchange/binance/futures/positions").json()["error"]["code"], "FUTURES_ACK_REQUIRED")
        self.assertTrue(self.user.post("/api/v1/exchange/futures/ack").json()["data"]["futuresAck"])
        self.assertTrue(self.user.get("/api/v1/exchange/config").json()["data"]["futuresAck"])
        self.assertEqual(self.user.post("/api/v1/exchange/binance/futures/leverage", json={"symbol": sym, "leverage": 6, "marginMode": "cross"}).json()["error"]["code"], "LEVERAGE_TOO_HIGH")
        r = self.user.post("/api/v1/exchange/okx/futures/leverage", json={"symbol": "BTC", "leverage": 3, "marginMode": "isolated"})
        self.assertEqual(r.json()["error"]["code"], "NOT_BOUND")
        r = self.user.post("/api/v1/exchange/binance/futures/leverage", json={"symbol": "BTC", "leverage": 3, "marginMode": "isolated"})
        self.assertEqual(r.json()["data"], {"symbol": sym, "leverage": 3, "marginMode": "isolated"})
        self.admin.put("/api/v1/admin/exchange", json={"maxLeverage": 10})
        self.assertEqual(self.user.post("/api/v1/exchange/binance/futures/leverage", json={"symbol": sym, "leverage": 8, "marginMode": "cross"}).status_code, 200)
        base = {"clientOrderId": "fut000000001", "symbol": sym, "side": "buy", "type": "limit", "amount": 0.4, "price": 100, "takeProfit": 120, "stopLoss": 90}
        r = self.user.post("/api/v1/exchange/binance/futures/orders", json=base).json()["data"]
        self.assertEqual([t["kind"] for t in r["tpsl"]], ["takeProfit", "stopLoss"])
        made = self._creates()
        self.assertEqual(len(made), 3)
        self.assertTrue(made[1][5]["reduceOnly"]); self.assertEqual(made[1][5]["takeProfitPrice"], 120); self.assertEqual(made[2][5]["stopLossPrice"], 90)
        self.assertTrue(self.user.post("/api/v1/exchange/binance/futures/orders", json=base).json()["data"]["duplicate"])
        self.assertEqual(len(self._creates()), 3)
        bad = {**base, "clientOrderId": "fut000000002", "takeProfit": 80}
        self.assertEqual(self.user.post("/api/v1/exchange/binance/futures/orders", json=bad).json()["error"]["code"], "BAD_TPSL")
        big = {**base, "clientOrderId": "fut000000003", "amount": 1, "takeProfit": None, "stopLoss": None}
        self.assertEqual(self.user.post("/api/v1/exchange/binance/futures/orders", json=big).json()["error"]["code"], "ORDER_TOO_LARGE")
        self.admin.put("/api/v1/admin/exchange", json={"maxFuturesUsdt": 500})
        self.assertEqual(self.user.post("/api/v1/exchange/binance/futures/orders", json=big).status_code, 200)
        # spot cap unchanged (50)
        spot = {"clientOrderId": "spot00000009", "symbol": "BTC/USDT", "side": "buy", "type": "limit", "amount": 1, "price": 100}
        self.assertEqual(self.user.post("/api/v1/exchange/binance/orders", json=spot).json()["error"]["code"], "ORDER_TOO_LARGE")
        self.admin.put("/api/v1/admin/exchange", json={"killSwitch": True})
        k = {**base, "clientOrderId": "fut000000004", "takeProfit": None, "stopLoss": None}
        self.assertEqual(self.user.post("/api/v1/exchange/binance/futures/orders", json=k).json()["error"]["code"], "KILL_SWITCH")
        FakeClient.positions = [{"symbol": sym, "side": "long", "contracts": 5, "entryPrice": 100}]
        ro = {**k, "clientOrderId": "fut000000005", "side": "sell", "reduceOnly": True, "amount": 5}
        self.assertEqual(self.user.post("/api/v1/exchange/binance/futures/orders", json=ro).status_code, 200)  # close allowed, cap n/a
        f = self.user.get("/api/v1/exchange/binance/futures/funding?symbol=BTC").json()["data"]
        self.assertEqual(f["fundingRate"], 0.0001)

    def test_futures_positions_close_and_admin_close_all(self):
        self._open_for_user(); self._bind()
        self.user.post("/api/v1/exchange/futures/ack")
        FakeClient.positions = [
            {"symbol": "BTC/USDT:USDT", "side": "long", "contracts": 0.2, "entryPrice": 100, "markPrice": 101, "liquidationPrice": 60, "unrealizedPnl": 0.2, "leverage": 3, "marginMode": "cross"},
            {"symbol": "ETH/USDT:USDT", "side": "short", "contracts": 1, "entryPrice": 10, "markPrice": 9, "liquidationPrice": 14, "unrealizedPnl": 1, "leverage": 2, "marginMode": "isolated"},
            {"symbol": "SOL/USDT:USDT", "side": "long", "contracts": 0},
        ]
        items = self.user.get("/api/v1/exchange/binance/futures/positions").json()["data"]["items"]
        self.assertEqual([p["symbol"] for p in items], ["BTC/USDT:USDT", "ETH/USDT:USDT"])
        self.assertEqual(items[0]["liquidationPrice"], 60)
        self.user.post("/api/v1/exchange/binance/futures/close", json={"symbol": "ETH/USDT:USDT", "clientOrderId": "closeeth0001", "confirmed": True})
        last = self._creates()[-1]
        self.assertEqual((last[0], last[1], last[2], last[3]), ("ETH/USDT:USDT", "market", "buy", 1.0)); self.assertTrue(last[5]["reduceOnly"])
        self.assertEqual(self.user.post("/api/v1/admin/exchange/close-all", json={"user": "exuser"}).status_code, 403)
        rep = self.admin.post("/api/v1/admin/exchange/close-all", json={"user": "exuser"}).json()["data"]["report"]
        self.assertEqual(sorted(r["symbol"] for r in rep), ["BTC/USDT:USDT", "ETH/USDT:USDT"])
        self.assertTrue(all(r["ok"] for r in rep), rep)
        self.assertIn("admin_close_all", self.admin.get("/api/v1/admin/exchange/audit").text)

    def test_bind_futures_perm_ok_transfer_rejected(self):
        self._open_for_user()
        FakeClient.restrictions = {**FakeClient.restrictions, "enableFutures": True}
        r = self._bind(); self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["data"]["perms"]["futures"])
        self.user.delete("/api/v1/exchange/binance/bind")
        FakeClient.restrictions = {**FakeClient.restrictions, "permitsUniversalTransfer": True}
        self.assertEqual(self._bind().json()["error"]["code"], "KEY_HAS_WITHDRAW")

    def test_standby_blocks_writes(self):
        self._open_for_user()
        os.environ["AUU_ROLE"] = "standby"
        self.assertEqual(self._bind().status_code, 503)


if __name__ == "__main__":
    unittest.main()


class AdminBypassTests(ExchangeApiTests):
    def test_admin_access_without_allowlist(self):
        self.admin.put("/api/v1/admin/exchange", json={"enabled": True})
        self.assertTrue(self.admin.get("/api/v1/exchange/config").json()["data"]["access"])
        self.assertFalse(self.user.get("/api/v1/exchange/config").json()["data"]["access"])
        self.admin.put("/api/v1/admin/exchange", json={"allowlistOnly": False})
        self.assertTrue(self.user.get("/api/v1/exchange/config").json()["data"]["access"])
