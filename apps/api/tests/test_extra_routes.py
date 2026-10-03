"""大盘 coin routes: /coin, /candles + /orderbook with venue, paper orders on a non-pool coin."""
from __future__ import annotations

import time
from unittest.mock import patch

import tests.test_mainstream_data as M

NOW = M.NOW


def _board(*bases):
    return {"status": "ok", "rows": [{"base": b, "symbol": b + "USDT", "last": 120.0, "change_24h": 0.05, "volume_24h": 5e7, "bid": 119.9, "ask": 120.1} for b in bases]}


class ExtraRouteTests(M.MainstreamApiTests):
    def setUp(self):
        super().setUp()
        from app.marketdata.extra import reset_extra
        from app.paper.mainstream_account import reset_accounts as reset_paper

        boards = {"binance": _board("PEPE", "WIF"), "okx": _board("PEPE"), "bybit": _board("ONLYBY"), "coinbase": _board()}
        self._p = [patch("app.marketdata.tickers.load_board", side_effect=lambda v: boards.get(v, {"status": "unavailable", "rows": []})),
                   patch("app.marketdata.tickers.peek_board", return_value=None)]
        for p in self._p:
            p.start()
        reset_extra()
        reset_paper()

    def tearDown(self):
        from app.marketdata.extra import reset_extra
        from app.paper.mainstream_account import reset_accounts as reset_paper

        for p in self._p:
            p.stop()
        reset_extra()
        reset_paper()
        super().tearDown()

    def test_coin_candles_book_and_validation(self):
        user = self._signed_in("xuser")
        anon = self.TestClient(self.app)
        self.assertEqual(anon.get("/api/v1/mainstream/coin?symbol=PEPE&venue=binance").status_code, 401)
        c = user.get("/api/v1/mainstream/coin?symbol=pepe&venue=binance").json()["data"]
        self.assertEqual((c["symbol"], c["dataVenue"], c["tradable"], c["inPool"]), ("PEPE", "binance", True, False))
        self.assertGreater(c["price"], 0)  # last 1m close from the venue
        self.assertEqual(c["change24h"], 0.05)
        only = user.get("/api/v1/mainstream/coin?symbol=ONLYBY&venue=bybit").json()["data"]
        self.assertTrue(only["viewOnly"])
        self.assertIsNone(only["dataVenue"])
        r = user.get("/api/v1/mainstream/candles?symbol=PEPE&venue=binance&tf=1h&limit=50")
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()["data"]
        self.assertEqual((d["exchange"], d["pair"], len(d["candles"])), ("binance", "PEPE/USDT", 50))
        self.assertEqual(d["candles"][-1]["ts"], NOW // 3_600_000 * 3_600_000)
        self.assertEqual(user.get("/api/v1/mainstream/candles?symbol=NOPE&venue=binance&tf=1h").status_code, 404)
        self.assertEqual(user.get("/api/v1/mainstream/candles?symbol=ONLYBY&venue=bybit&tf=1h").json()["error"]["code"], "VIEW_ONLY")
        self.assertEqual(user.get("/api/v1/mainstream/candles?symbol=PEPE&venue=kraken&tf=1h").status_code, 400)
        self.assertEqual(user.get("/api/v1/mainstream/candles?symbol=../x&tf=1h").status_code, 400)
        self.assertEqual(user.get("/api/v1/mainstream/candles?symbol=DOGE&tf=1h").status_code, 404)  # not listed anywhere
        # pool coin without venue still reads the strategy store
        self.assertNotIn("extra", user.get("/api/v1/mainstream/candles?symbol=BTC&tf=1h&limit=5").json()["data"])
        b = user.get("/api/v1/mainstream/orderbook?symbol=PEPE&venue=binance").json()["data"]
        self.assertEqual((b["exchange"], b["pair"]), ("binance", "PEPE/USDT"))
        self.assertEqual(user.get("/api/v1/mainstream/orderbook?symbol=NOPE&venue=okx").status_code, 404)

    def test_paper_order_on_non_pool_coin_same_limits(self):
        user = self._signed_in("xtrader")
        with patch("app.paper.mainstream_account.time.time", return_value=NOW / 1000):
            big = user.post("/api/v1/mainstream/paper/orders", json={"client_order_id": "extra-order-0001", "symbol": "PEPE", "side": "buy", "notional": 2500})
            self.assertEqual(big.json()["error"]["code"], "ORDER_CAP", big.text)  # same 2,000 USDT per-order cap
            ok = user.post("/api/v1/mainstream/paper/orders", json={"client_order_id": "extra-order-0002", "symbol": "PEPE", "side": "buy", "notional": 500})
        self.assertEqual(ok.status_code, 200, ok.text)
        with patch("app.paper.mainstream_account.time.time", return_value=NOW / 1000):
            codes = []
            for i, n in enumerate((1500, 1600, 1700)):  # 500 held -> 2000 -> 3600 -> 5300 > 5,000 per-coin cap
                r = user.post("/api/v1/mainstream/paper/orders", json={"client_order_id": f"extra-cap-{i:04d}", "symbol": "PEPE", "side": "buy", "notional": n})
                codes.append("ok" if r.status_code == 200 else r.json()["error"]["code"])
        self.assertEqual(codes, ["ok", "ok", "POSITION_CAP"])
        bad = user.post("/api/v1/mainstream/paper/orders", json={"client_order_id": "extra-order-0003", "symbol": "NOPE", "side": "buy", "notional": 100})
        self.assertEqual(bad.status_code, 404)
        acct = user.get("/api/v1/mainstream/paper/account?symbol=PEPE").json()["data"]
        self.assertTrue(any(p["symbol"] == "PEPE" for p in acct.get("positions", [])), acct.get("positions"))


for _n in [n for n in dir(M.MainstreamApiTests) if n.startswith("test_")]:
    setattr(ExtraRouteTests, _n, None)  # inherited API tests already run in their own class
