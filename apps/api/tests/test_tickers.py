"""Batch CEX tickers and independent venue failure."""
from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from app.marketdata.fetch import UpstreamError
from app.marketdata.tickers import reset_ticker_cache


def _binance():
    return [
        {
            "symbol": "SOLUSDT",
            "lastPrice": "150",
            "priceChangePercent": "2.5",
            "quoteVolume": "5000000",
            "bidPrice": "149.9",
            "askPrice": "150.1",
        },
        {
            "symbol": "BTCUSDT",
            "lastPrice": "80000",
            "priceChangePercent": "-1",
            "quoteVolume": "9000000",
            "bidPrice": "79990",
            "askPrice": "80010",
        },
        {
            "symbol": "ETHUPUSDT",
            "lastPrice": "3",
            "priceChangePercent": "10",
            "quoteVolume": "99999999",
            "bidPrice": "3",
            "askPrice": "3",
        },
    ]


def _okx():
    return {
        "data": [
            {
                "instId": "SOL-USDT",
                "last": "151",
                "open24h": "150",
                "volCcy24h": "4000000",
                "bidPx": "150.8",
                "askPx": "151.2",
            }
        ]
    }


def _bybit():
    return {
        "result": {
            "list": [
                {
                    "symbol": "SOLUSDT",
                    "lastPrice": "149",
                    "price24hPcnt": "-0.01",
                    "turnover24h": "3000000",
                    "bid1Price": "148.9",
                    "ask1Price": "149.1",
                }
            ]
        }
    }


def _coinbase():
    return {
        "products": [
            {
                "product_id": "SOL-USD",
                "base_currency_id": "SOL",
                "quote_currency_id": "USD",
                "price": "150.5",
                "price_percentage_change_24h": "1.2",
                "volume_24h": "1000",
                "status": "online",
            }
        ],
        "pagination": {"has_next": False},
    }


class TickerBoardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        from app.main import app

        cls.client = TestClient(app)

    def setUp(self):
        reset_ticker_cache()

    def test_reader_has_no_order_path(self):
        text = Path("app/marketdata/tickers.py").read_text(encoding="utf-8").lower()
        self.assertNotIn("/order", text)
        self.assertNotIn("apikey", text)
        self.assertNotIn("app.live", text)

    def test_top_volume_and_one_venue_down(self):
        def fake(url, timeout=None):
            if "binance" in url:
                raise UpstreamError("unavailable", "timeout")
            if "okx" in url:
                return _okx()
            if "bybit" in url:
                return _bybit()
            if "coinbase.com" in url or "brokerage" in url:
                return _coinbase()
            raise UpstreamError("unavailable", url)

        with patch("app.marketdata.tickers.get_json", side_effect=fake):
            board = self.client.get("/api/v1/majors/tickers", params={"venue": "binance"}).json()["data"]
            okx = self.client.get("/api/v1/majors/tickers", params={"venue": "okx", "bucket": "gainers"}).json()["data"]
            losers = self.client.get("/api/v1/majors/tickers", params={"venue": "bybit", "bucket": "losers"}).json()["data"]
        self.assertFalse(board["liveEnabled"])
        self.assertEqual(board["status_label"], "不可用")
        self.assertEqual(board["items"], [])
        health = {row["id"]: row["status_label"] for row in board["venues"]}
        self.assertEqual(health["binance"], "不可用")
        self.assertEqual(health["okx"], "可用")
        self.assertEqual(health["bybit"], "可用")
        self.assertEqual(health["coinbase"], "可用")
        self.assertEqual([row["base"] for row in okx["items"]], ["SOL"])
        self.assertAlmostEqual(okx["items"][0]["change_24h"], 1 / 150)
        self.assertEqual(losers["items"][0]["base"], "SOL")
        self.assertAlmostEqual(losers["items"][0]["change_24h"], -0.01)

    def test_cross_spread_uses_best_bid_and_ask(self):
        def fake(url, timeout=None):
            if "binance" in url:
                return _binance()
            if "okx" in url:
                return _okx()
            if "bybit" in url:
                raise UpstreamError("unavailable", "timeout")
            return _coinbase()

        with patch("app.marketdata.tickers.get_json", side_effect=fake):
            data = self.client.get("/api/v1/majors/tickers", params={"venue": "cross", "q": "SOL"}).json()["data"]
            ranked = self.client.get("/api/v1/majors/tickers", params={"venue": "binance"}).json()["data"]
        self.assertFalse(data["liveEnabled"])
        self.assertEqual(data["status_label"], "可用")
        self.assertEqual(len(data["items"]), 1)
        row = data["items"][0]
        self.assertEqual(row["base"], "SOL")
        self.assertEqual(row["bid_venue"], "OKX")
        self.assertEqual(row["ask_venue"], "Binance")
        self.assertAlmostEqual(row["bid"], 150.8)
        self.assertAlmostEqual(row["ask"], 150.1)
        self.assertGreater(row["spread_bps"], 0)
        bases = [item["base"] for item in ranked["items"]]
        self.assertEqual(bases[0], "BTC")
        self.assertNotIn("ETHUP", bases)
