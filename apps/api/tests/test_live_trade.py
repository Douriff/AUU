"""实盘视图的服务端校验：可用性（总开关 / 白名单 / 未绑定 / 无交易权限 / 非支持品种 / 紧急停止）、
二次确认、reduce-only 平仓、clientOrderId 幂等与防双击、只用自己的钥匙、策略永不碰用户钥匙。

只用 mock 的 ccxt 客户端（tests.test_exchange_connect.FakeClient），socket 连接被禁用：绝不连真实交易所，不含任何真实钥匙。"""
from __future__ import annotations

import base64
import os
import re
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from app.auth.accounts import reset_accounts
from app.exchange import connector, store
from app.exchange import futures as fut
from app.exchange import service as svc
from tests.test_exchange_connect import FakeClient, KEYS, PW, SECRET, RequestTimeout, fake_make

SYM = "BTC/USDT:USDT"
MARKETS = {
    "BTC/USDT": {"spot": True, "swap": False, "quote": "USDT", "active": True},
    "ETH/USDT": {"spot": True, "swap": False, "quote": "USDT", "active": True},
    "OLD/USDT": {"spot": True, "swap": False, "quote": "USDT", "active": False},
    "BTC/USDT:USDT": {"spot": False, "swap": True, "linear": True, "quote": "USDT", "active": True},
    "ETH/USDT:USDT": {"spot": False, "swap": True, "linear": True, "quote": "USDT", "active": True},
}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.root = root
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
        FakeClient.restrictions = {"enableReading": True, "enableSpotAndMarginTrading": True, "enableFutures": True,
                                   "enableWithdrawals": False, "ipRestrict": True}
        FakeClient.okx_perm = {"perm": "read_only,trade", "ip": "156.227.238.242"}
        FakeClient.markets = MARKETS
        self.sock = mock.patch.object(socket.socket, "connect", side_effect=AssertionError("network access in test"))
        self.sock.start()
        self.mk = mock.patch.object(connector, "make_client", side_effect=fake_make)
        self.mk.start()
        connector._MARKETS.clear()
        from fastapi.testclient import TestClient
        from app.main import app
        self.TC, self.app = TestClient, app
        self.admin = self._reg("exadmin")  # first user is admin
        self.user = self._reg("exuser")
        self.other = self._reg("exother")

    def tearDown(self):
        self.mk.stop(); self.sock.stop()
        if "markets" in FakeClient.__dict__:
            del FakeClient.markets
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

    def open_for(self, *names):
        self.assertEqual(self.admin.put("/api/v1/admin/exchange", json={"enabled": True}).status_code, 200)
        for n in names:
            self.assertEqual(self.admin.post("/api/v1/admin/exchange/allowlist", json={"user": n, "allow": True}).status_code, 200)

    def bind(self, client, ex="binance", key="AKEYabcdefgh1234"):
        r = client.post(f"/api/v1/exchange/{ex}/bind", json={"apiKey": key, "secret": SECRET, "passphrase": "pp123456"})
        self.assertEqual(r.status_code, 200, r.text)

    def ready(self):
        """enabled, user allowlisted + bound (spot + futures key), futures risk acknowledged"""
        self.open_for("exuser")
        self.bind(self.user)
        self.user.post("/api/v1/exchange/futures/ack")

    def avail(self, client=None, ex="binance"):
        r = (client or self.user).get("/api/v1/exchange/availability")
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["data"]["exchanges"][ex]

    def creates(self):
        return [x for c in FakeClient.instances for x in c.created]

    @staticmethod
    def spot(coid="spot00000001", **kw):
        return {"clientOrderId": coid, "symbol": "BTC/USDT", "side": "buy", "type": "limit", "amount": 0.1, "price": 100, "confirmed": True, **kw}

    @staticmethod
    def swap(coid="swap00000001", **kw):
        return {"clientOrderId": coid, "symbol": SYM, "side": "buy", "type": "limit", "amount": 0.1, "price": 100, "confirmed": True, **kw}

    def err(self, r):
        self.assertGreaterEqual(r.status_code, 400, r.text)
        return r.json()["error"]["code"]


class AvailabilityTests(Base):
    def test_requires_login(self):
        self.assertEqual(self.TC(self.app).get("/api/v1/exchange/availability").status_code, 401)

    def test_every_unavailable_state_has_a_reason(self):
        a = self.avail()  # master switch off by default
        for m in ("spot", "swap"):
            for k in ("open", "close"):
                self.assertEqual((a[m][k]["available"], a[m][k]["reason"]), (False, "EXCHANGE_OFF"))
                self.assertTrue(a[m][k]["message"])
        self.admin.put("/api/v1/admin/exchange", json={"enabled": True})
        self.assertEqual(self.avail()["spot"]["open"]["reason"], "EXCHANGE_NOT_ALLOWED")
        self.open_for("exuser")
        self.assertEqual(self.avail()["spot"]["open"]["reason"], "NOT_BOUND")
        self.assertFalse(self.avail()["bound"])
        # a read-only key: viewing works, trading is "unavailable"
        FakeClient.restrictions = {**FakeClient.restrictions, "enableSpotAndMarginTrading": False, "enableFutures": False}
        self.bind(self.user)
        a = self.avail()
        self.assertEqual(a["spot"]["open"]["reason"], "KEY_NO_TRADE")
        self.assertEqual(a["swap"]["open"]["reason"], "FUTURES_ACK_REQUIRED")
        self.user.post("/api/v1/exchange/futures/ack")
        self.assertEqual(self.avail()["swap"]["open"]["reason"], "KEY_NO_FUTURES")
        self.assertEqual(self.avail()["swap"]["close"]["reason"], "KEY_NO_FUTURES")
        # the other exchange is not bound
        self.assertEqual(self.avail(ex="okx")["spot"]["open"]["reason"], "NOT_BOUND")

    def test_available_and_kill_switch_keeps_futures_close(self):
        self.ready()
        a = self.avail()
        self.assertTrue(all(a[m][k]["available"] for m in ("spot", "swap") for k in ("open", "close")), a)
        self.admin.put("/api/v1/admin/exchange", json={"killSwitch": True})
        a = self.avail()
        self.assertEqual(a["spot"]["open"]["reason"], "KILL_SWITCH")
        self.assertEqual(a["swap"]["open"]["reason"], "KILL_SWITCH")
        self.assertTrue(a["swap"]["close"]["available"])  # reduce-only close stays possible in an emergency
        self.assertEqual(self.avail(self.other)["spot"]["open"]["reason"], "EXCHANGE_NOT_ALLOWED")  # per user

    def test_standby_node(self):
        self.ready()
        os.environ["AUU_ROLE"] = "standby"
        self.assertEqual(self.avail()["spot"]["open"]["reason"], "STANDBY")


class ServerSideEnforcementTests(Base):
    """The same states refuse real order requests: the UI is never the only guard."""

    def post_all(self, coid):
        return {
            "spot": self.user.post("/api/v1/exchange/binance/orders", json=self.spot(coid)),
            "swap": self.user.post("/api/v1/exchange/binance/futures/orders", json=self.swap(coid)),
            "close": self.user.post("/api/v1/exchange/binance/futures/close", json={"symbol": SYM, "clientOrderId": coid, "confirmed": True}),
        }

    def test_master_switch_off_and_not_allowlisted(self):
        for code in ("EXCHANGE_OFF", "EXCHANGE_NOT_ALLOWED"):
            if code == "EXCHANGE_NOT_ALLOWED":
                self.admin.put("/api/v1/admin/exchange", json={"enabled": True})
            for kind, r in self.post_all("gate0000001").items():
                self.assertEqual((r.status_code, self.err(r)), (403, code), kind)
        self.assertEqual(self.creates(), [])

    def test_not_bound_and_no_trade_permission(self):
        self.open_for("exuser")
        for kind, r in self.post_all("unbound00001").items():
            self.assertEqual(self.err(r), "NOT_BOUND", kind)
        FakeClient.restrictions = {**FakeClient.restrictions, "enableSpotAndMarginTrading": False, "enableFutures": False}
        self.bind(self.user)
        self.user.post("/api/v1/exchange/futures/ack")
        r = self.post_all("readonly0001")
        self.assertEqual(self.err(r["spot"]), "KEY_NO_TRADE")
        self.assertEqual(self.err(r["swap"]), "KEY_NO_FUTURES")
        self.assertEqual(self.err(r["close"]), "KEY_NO_FUTURES")
        self.assertEqual(self.creates(), [])

    def test_futures_need_ack(self):
        self.open_for("exuser")
        self.bind(self.user)
        r = self.post_all("noack0000001")
        self.assertEqual(self.err(r["swap"]), "FUTURES_ACK_REQUIRED")
        self.assertEqual(self.err(r["close"]), "FUTURES_ACK_REQUIRED")
        self.assertEqual([c for c in self.creates() if ":" in c[0]], [])  # the spot order is fine; no futures order was sent

    def test_kill_switch_blocks_opening_but_not_reduce_only_close(self):
        self.ready()
        FakeClient.positions = [{"symbol": SYM, "side": "long", "contracts": 0.3, "entryPrice": 100, "marginMode": "isolated"}]
        self.admin.put("/api/v1/admin/exchange", json={"killSwitch": True})
        r = self.post_all("kill00000001")
        self.assertEqual(self.err(r["spot"]), "KILL_SWITCH")
        self.assertEqual(self.err(r["swap"]), "KILL_SWITCH")
        self.assertEqual(r["close"].status_code, 200, r["close"].text)
        self.assertEqual(len(self.creates()), 1)

    def test_unsupported_symbols_refused(self):
        self.ready()
        for sym in ("OLD/USDT", "XYZ/USDT"):  # inactive / not listed
            r = self.user.post("/api/v1/exchange/binance/orders", json=self.spot("unsup0000001", symbol=sym))
            self.assertEqual(self.err(r), "SYMBOL_UNSUPPORTED", sym)
        r = self.user.post("/api/v1/exchange/binance/futures/orders", json=self.swap("unsup0000002", symbol="XYZ/USDT:USDT"))
        self.assertEqual(self.err(r), "SYMBOL_UNSUPPORTED")
        r = self.user.post("/api/v1/exchange/binance/orders", json=self.spot("unsup0000003", symbol="BTC/ETH"))
        self.assertEqual(self.err(r), "BAD_SYMBOL")  # not an XXX/USDT spot pair
        r = self.user.post("/api/v1/exchange/binance/futures/orders", json=self.swap("unsup0000004", symbol="BTC/USDC:USDC"))
        self.assertEqual(self.err(r), "BAD_SYMBOL")  # only USDT-margined perpetuals
        self.assertEqual(self.user.get("/api/v1/exchange/binance/quote?symbol=XYZ&market=spot").json()["error"]["code"], "SYMBOL_UNSUPPORTED")
        self.assertEqual(self.creates(), [])

    def test_second_confirmation_is_required_by_the_server(self):
        self.ready()
        FakeClient.positions = [{"symbol": SYM, "side": "long", "contracts": 0.3}]
        for url, body in (("/api/v1/exchange/binance/orders", self.spot("conf00000001")),
                          ("/api/v1/exchange/binance/futures/orders", self.swap("conf00000002")),
                          ("/api/v1/exchange/binance/futures/close", {"symbol": SYM, "clientOrderId": "conf00000003", "confirmed": True})):
            body = dict(body); body.pop("confirmed")
            self.assertEqual(self.err(self.user.post(url, json=body)), "CONFIRM_REQUIRED", url)
            self.assertEqual(self.err(self.user.post(url, json={**body, "confirmed": False})), "CONFIRM_REQUIRED", url)
        self.assertEqual(self.creates(), [])
        self.assertEqual(self.user.post("/api/v1/exchange/binance/orders", json=self.spot("conf00000001")).status_code, 200)

    def test_withdraw_capable_key_can_never_trade(self):
        self.ready()
        row = store.get_cred(next(u["id"] for u in [self._me(self.user)]), "binance")
        store.put_cred(self._me(self.user)["id"], "binance", row["blob"], row["masked"], {**row["perms"], "withdraw": True})
        self.assertEqual(self.err(self.user.post("/api/v1/exchange/binance/orders", json=self.spot("wd0000000001"))), "KEY_HAS_WITHDRAW")

    def _me(self, c):
        return c.get("/api/v1/auth/me").json()["data"]["user"]

    def test_unauthenticated_orders_401(self):
        anon = self.TC(self.app)
        for url in ("/api/v1/exchange/binance/orders", "/api/v1/exchange/binance/futures/orders", "/api/v1/exchange/binance/futures/close"):
            self.assertEqual(anon.post(url, json={}).status_code, 401)
        self.assertEqual(anon.get("/api/v1/exchange/binance/quote?symbol=BTC").status_code, 401)


class FuturesCloseTests(Base):
    def setUp(self):
        super().setUp()
        self.ready()
        FakeClient.positions = [{"symbol": SYM, "side": "long", "contracts": 0.25, "entryPrice": 100, "markPrice": 101,
                                 "leverage": 3, "marginMode": "isolated"}]

    def close(self, coid="close0000001", **kw):
        return self.user.post("/api/v1/exchange/binance/futures/close", json={"symbol": SYM, "clientOrderId": coid, "confirmed": True, **kw})

    def test_close_is_reduce_only_market_with_server_side_side_and_size(self):
        r = self.close()
        self.assertEqual(r.status_code, 200, r.text)
        (sym, otype, side, amount, price, params), = self.creates()
        self.assertEqual((sym, otype, side, amount, price), (SYM, "market", "sell", 0.25, None))
        self.assertIs(params["reduceOnly"], True)
        self.assertEqual(params["clientOrderId"], "auuclose0000001")
        self.assertEqual(params["marginMode"], "isolated")
        self.assertFalse(r.json()["data"]["duplicate"])

    def test_client_cannot_choose_side_or_size(self):
        for extra in ({"side": "buy"}, {"amount": 100}, {"reduceOnly": False}):
            self.assertEqual(self.close("closeextra001", **extra).status_code, 422)
        self.assertEqual(self.creates(), [])

    def test_short_position_is_closed_with_a_buy(self):
        FakeClient.positions = [{"symbol": SYM, "side": "short", "contracts": -2, "entryPrice": 100}]
        self.assertEqual(self.close().status_code, 200)
        self.assertEqual(self.creates()[0][2:4], ("buy", 2.0))

    def test_idempotent_same_client_order_id(self):
        r1, r2 = self.close(), self.close()
        self.assertEqual((r1.status_code, r2.status_code), (200, 200))
        self.assertFalse(r1.json()["data"]["duplicate"])
        self.assertTrue(r2.json()["data"]["duplicate"])
        self.assertEqual(len(self.creates()), 1)

    def test_new_id_with_no_position_is_not_sent(self):
        self.assertEqual(self.close("closefirst001").status_code, 200)
        FakeClient.positions = []
        r = self.close("closesecond01")
        self.assertEqual((r.status_code, self.err(r)), (404, "NO_POSITION"))
        self.assertEqual(len(self.creates()), 1)

    def test_concurrent_double_click_sends_once(self):
        uid = self._uid()
        out, gate = [], threading.Barrier(8)

        def go():
            gate.wait()
            try:
                out.append(fut.close_position(uid, "binance", SYM, "closerace0001", True))
            except svc.ServiceError as e:
                out.append(e.code)

        ts = [threading.Thread(target=go) for _ in range(8)]
        [t.start() for t in ts]; [t.join() for t in ts]
        self.assertEqual(len(self.creates()), 1, out)
        self.assertEqual(sum(1 for o in out if isinstance(o, dict) and not o["duplicate"]), 1)

    def _uid(self):
        return str(self.user.get("/api/v1/auth/me").json()["data"]["user"]["id"])

    def test_timeout_is_looked_up_not_resent(self):
        FakeClient.create_exc = RequestTimeout("timeout")
        r = self.close("closetimeout1")
        self.assertEqual(self.err(r), "ORDER_UNKNOWN")
        FakeClient.create_exc = None
        r = self.close("closetimeout1")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["data"]["duplicate"])
        self.assertEqual(self.creates(), [])  # nothing was re-sent; the order was found by clientOrderId

    def test_hedge_mode_closes_both_legs_reduce_only(self):
        FakeClient.positions = [{"symbol": SYM, "side": "long", "contracts": 1}, {"symbol": SYM, "side": "short", "contracts": 2}]
        self.assertEqual(self.close("closehedge001").status_code, 200)
        sides = sorted(c[2] for c in self.creates())
        self.assertEqual(sides, ["buy", "sell"])
        self.assertTrue(all(c[5]["reduceOnly"] for c in self.creates()))

    def test_reduce_only_flag_on_normal_endpoint_is_validated(self):
        base = self.swap("ro0000000001", side="sell", type="market", price=None, amount=0.1, reduceOnly=True)
        self.assertEqual(self.user.post("/api/v1/exchange/binance/futures/orders", json=base).status_code, 200)
        self.assertIs(self.creates()[0][5]["reduceOnly"], True)
        too_big = {**base, "clientOrderId": "ro0000000002", "amount": 5}
        self.assertEqual(self.err(self.user.post("/api/v1/exchange/binance/futures/orders", json=too_big)), "REDUCE_EXCEEDS_POSITION")
        wrong_way = {**base, "clientOrderId": "ro0000000003", "side": "buy", "amount": 0.1}  # buying cannot reduce a long
        self.assertEqual(self.err(self.user.post("/api/v1/exchange/binance/futures/orders", json=wrong_way)), "NO_POSITION")
        FakeClient.positions = []
        none = {**base, "clientOrderId": "ro0000000004"}
        self.assertEqual(self.err(self.user.post("/api/v1/exchange/binance/futures/orders", json=none)), "NO_POSITION")
        self.assertEqual(len(self.creates()), 1)

    def test_leverage_and_margin_mode_are_validated_and_applied_before_opening(self):
        ok = self.swap("lev000000001", type="market", price=None, leverage=3, marginMode="isolated")
        r = self.user.post("/api/v1/exchange/binance/futures/orders", json=ok)
        self.assertEqual(r.status_code, 200, r.text)
        applied = [x for c in FakeClient.instances for x in c.leverage]
        self.assertIn(("lev", 3, SYM, None), applied)
        self.assertIn(("mode", "isolated", SYM, None), applied)
        n = len(self.creates())
        for bad, code in (({"leverage": 6, "marginMode": "cross"}, "LEVERAGE_TOO_HIGH"), ({"leverage": 0, "marginMode": "cross"}, "LEVERAGE_TOO_HIGH"),
                          ({"leverage": 2, "marginMode": "weird"}, "BAD_MARGIN_MODE"), ({"leverage": 2}, "BAD_MARGIN_MODE")):
            r = self.user.post("/api/v1/exchange/binance/futures/orders", json=self.swap(f"lev{len(code)}{bad['leverage']}abcdef", **bad))
            self.assertEqual(self.err(r), code, bad)
        self.assertEqual(len(self.creates()), n)


class SpotIdempotencyTests(Base):
    def setUp(self):
        super().setUp()
        self.ready()

    def test_same_client_order_id_once(self):
        a = self.user.post("/api/v1/exchange/binance/orders", json=self.spot())
        b = self.user.post("/api/v1/exchange/binance/orders", json=self.spot())
        self.assertEqual((a.status_code, b.status_code), (200, 200))
        self.assertTrue(b.json()["data"]["duplicate"])
        self.assertEqual(len(self.creates()), 1)
        self.assertEqual(self.creates()[0][5]["clientOrderId"], "auuspot00000001")

    def test_identical_order_with_other_id_is_a_double_click_conflict(self):
        self.assertEqual(self.user.post("/api/v1/exchange/binance/orders", json=self.spot("dbl000000001")).status_code, 200)
        r = self.user.post("/api/v1/exchange/binance/orders", json=self.spot("dbl000000002"))
        self.assertEqual((r.status_code, self.err(r)), (409, "DUPLICATE_ORDER"))
        self.assertEqual(len(self.creates()), 1)
        self.assertEqual(self.user.post("/api/v1/exchange/binance/orders", json=self.spot("dbl000000003", amount=0.2)).status_code, 200)
        r = self.user.post("/api/v1/exchange/binance/futures/orders", json=self.swap("dbl000000004"))
        self.assertEqual(r.status_code, 200)  # spot and futures are separate books
        self.assertEqual(self.err(self.user.post("/api/v1/exchange/binance/futures/orders", json=self.swap("dbl000000005"))), "DUPLICATE_ORDER")

    def test_threads_same_id_send_once(self):
        uid = str(self.user.get("/api/v1/auth/me").json()["data"]["user"]["id"])
        out, gate = [], threading.Barrier(8)

        def go():
            gate.wait()
            try:
                out.append(svc.place(uid, "binance", self.spot("race00000001")))
            except svc.ServiceError as e:
                out.append(e.code)

        ts = [threading.Thread(target=go) for _ in range(8)]
        [t.start() for t in ts]; [t.join() for t in ts]
        self.assertEqual(len(self.creates()), 1, out)

    def test_order_cap_still_applies_to_opening_but_not_reduce_only(self):
        big = self.spot("cap000000001", amount=10)  # 10 * 100 = 1000 > 50
        self.assertEqual(self.err(self.user.post("/api/v1/exchange/binance/orders", json=big)), "ORDER_TOO_LARGE")
        FakeClient.positions = [{"symbol": SYM, "side": "long", "contracts": 50}]
        r = self.user.post("/api/v1/exchange/binance/futures/orders", json=self.swap("cap000000002", side="sell", type="market", price=None, amount=50, reduceOnly=True))
        self.assertEqual(r.status_code, 200, r.text)

    def test_supported_lists_unsupported_coins_as_false(self):
        r = self.user.get("/api/v1/exchange/binance/supported?symbols=BTC,OLD,XYZ,ETH&market=spot").json()["data"]["supported"]
        self.assertEqual(r, {"BTC": True, "OLD": False, "XYZ": False, "ETH": True})
        r = self.user.get("/api/v1/exchange/binance/supported?symbols=BTC,XYZ&market=swap").json()["data"]["supported"]
        self.assertEqual(r, {"BTC": True, "XYZ": False})

    def test_quote_for_the_confirmation_dialog(self):
        q = self.user.get("/api/v1/exchange/binance/quote?symbol=BTC&market=spot").json()["data"]
        self.assertEqual((q["symbol"], q["bid"], q["ask"]), ("BTC/USDT", 99.0, 100.0))
        q = self.user.get("/api/v1/exchange/binance/quote?symbol=BTC&market=swap").json()["data"]
        self.assertEqual((q["symbol"], q["market"]), (SYM, "swap"))


class OwnKeyAndIsolationTests(Base):
    def test_each_user_trades_only_with_their_own_key(self):
        self.open_for("exuser", "exother")
        self.bind(self.user, key="USERKEYaaaaaaaa1")
        self.bind(self.other, key="OTHERKEYbbbbbbb2")
        self.assertEqual(self.user.post("/api/v1/exchange/binance/orders", json=self.spot("own000000001")).status_code, 200)
        self.assertEqual(self.other.post("/api/v1/exchange/binance/orders", json=self.spot("own000000002", amount=0.2)).status_code, 200)
        used = {c.creds["apiKey"] for c in FakeClient.instances if c.created}
        self.assertEqual(used, {"USERKEYaaaaaaaa1", "OTHERKEYbbbbbbb2"})
        by_key = {c.creds["apiKey"]: [x[5]["clientOrderId"] for x in c.created] for c in FakeClient.instances if c.created}
        self.assertEqual(by_key["USERKEYaaaaaaaa1"], ["auuown000000001"])
        # a third user without a key cannot ride on someone else's
        third = self._reg("exthird")
        self.admin.post("/api/v1/admin/exchange/allowlist", json={"user": "exthird", "allow": True})
        self.assertEqual(self.err(third.post("/api/v1/exchange/binance/orders", json=self.spot("own000000003"))), "NOT_BOUND")
        # same clientOrderId from two users is two independent orders
        self.assertEqual(self.other.post("/api/v1/exchange/binance/orders", json=self.spot("own000000001")).json()["data"]["duplicate"], False)

    def test_keys_are_stored_encrypted_and_never_returned_or_logged(self):
        self.open_for("exuser")
        self.bind(self.user)
        self.user.post("/api/v1/exchange/binance/orders", json=self.spot("enc000000001"))
        raw = (self.root / "exchange.sqlite").read_bytes()
        for wal in self.root.glob("exchange.sqlite-*"):
            raw += wal.read_bytes()
        self.assertNotIn(SECRET.encode(), raw)
        self.assertNotIn(b"AKEYabcdefgh1234", raw)
        for path in ("/api/v1/exchange/accounts", "/api/v1/exchange/config", "/api/v1/exchange/availability", "/api/v1/admin/exchange/audit"):
            txt = (self.admin if "admin" in path else self.user).get(path).text
            self.assertNotIn(SECRET, txt, path)
            self.assertNotIn("AKEYabcdefgh1234", txt, path)

    def test_strategies_never_touch_user_keys(self):
        """No module outside the exchange package + its route imports it, and no strategy / paper / backtest code can place orders."""
        root = Path(__file__).resolve().parents[1] / "app"
        allowed = {root / "routes" / "exchange.py"}
        pat = re.compile(r"(from\s+app\.exchange|import\s+app\.exchange|from\s+app\s+import\s+exchange|exchange\.(service|futures|store|connector))")
        for f in root.rglob("*.py"):
            if "exchange" in f.relative_to(root).parts[:1] or f in allowed:
                continue
            self.assertIsNone(pat.search(f.read_text(encoding="utf-8")), f"{f} must not use the user-exchange package")
        for pkg in ("strategies", "paper", "backtest", "pipeline", "risk", "live"):
            for f in (root / pkg).rglob("*.py"):
                src = f.read_text(encoding="utf-8")
                for bad in ("create_order", "ccxt.binance", "ccxt.okx", "make_client", "user_client", "AUU_EXCHANGE_MASTER_KEY"):
                    self.assertNotIn(bad, src, f"{f} contains {bad}")
        # only the session-gated HTTP routes can reach the order functions
        route_src = (root / "routes" / "exchange.py").read_text(encoding="utf-8")
        self.assertIn("_need(request", route_src)
        for name in ("place", "futures_place", "futures_close"):
            self.assertRegex(route_src, rf"def {name}\(.*?\n    try:\n        u = _need\(request, kind=\"write\"\)")

    def test_no_real_exchange_client_is_constructed(self):
        self.ready_user = None
        self.open_for("exuser")
        self.bind(self.user)
        self.user.post("/api/v1/exchange/binance/orders", json=self.spot("net000000001"))
        self.assertTrue(FakeClient.instances)  # every client came from the mock
        self.assertTrue(all(type(c).__name__ == "FakeClient" for c in FakeClient.instances))


if __name__ == "__main__":
    unittest.main()
