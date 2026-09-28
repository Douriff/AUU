"""Market universe: pump.fun lists, DexScreener changes, mock flag."""
from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import patch

from app.marketdata.fetch import UpstreamError
from app.marketdata.universe import reset_universe_cache
from app.providers import reset_provider
from app.providers.pumpfun_curve_math import INITIAL_REAL_TOKEN_RESERVES
from app.strategies.pump_paper_v1 import reset_engine

CURVE_MINT = "CurveMint111111111111111111111111111111"
GRAD_MINT = "GradMint1111111111111111111111111111111"


def _curve() -> dict:
    return {
        "mint": CURVE_MINT,
        "name": "Curve Cat",
        "symbol": "CCAT",
        "image_uri": "https://img.example/ccat.png",
        "program": "pump",
        "chain_id": "solana:main",
        "virtual_sol_reserves": 40_000_000_000,
        "virtual_token_reserves": 800_000_000_000_000,
        "real_token_reserves": int(INITIAL_REAL_TOKEN_RESERVES * 0.2),
        "total_supply": 1_000_000_000_000_000,
        "base_decimals": 6,
        "market_cap": 80.0,
        "usd_market_cap": 9_600.0,
        "created_timestamp": 1_790_000_000_000,
        "complete": False,
    }


def _grad() -> dict:
    return {
        "mint": GRAD_MINT,
        "name": "Graduated",
        "symbol": "GRAD",
        "image_uri": "https://img.example/grad.png",
        "program": "pump",
        "chain_id": "solana:main",
        "complete": True,
        "pump_swap_pool": "pool",
        "virtual_sol_reserves": 115_005_359_057,
        "virtual_token_reserves": 279_900_000_000_000,
        "total_supply": 1_000_000_000_000_000,
        "base_decimals": 6,
        "market_cap": 4_000.0,
        "usd_market_cap": 480_000.0,
        "created_timestamp": 1_700_000_000_000,
        "holder_count": 1200,
    }


def _dex_batch() -> list:
    return [
        {
            "chainId": "solana",
            "dexId": "pumpswap",
            "baseToken": {"address": GRAD_MINT, "symbol": "GRAD", "name": "Graduated"},
            "quoteToken": {"symbol": "SOL", "address": "So11111111111111111111111111111111111111112"},
            "priceUsd": "0.48",
            "priceNative": "0.004",
            "priceChange": {"m5": 1.5, "h1": -3.0, "h24": 20.0},
            "volume": {"h24": 9000},
            "liquidity": {"usd": 50000},
        }
    ]


class UniverseFeedTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["DATA_PROVIDER"] = "pumpfun_paper"
        os.environ["PUMP_PAPER_LOOP"] = "0"
        os.environ["PUMPFUN_DISCOVERY"] = "off"
        os.environ.pop("PUMPFUN_WATCH_MINTS", None)
        os.environ.pop("AUU_UNIVERSE_MOCK", None)
        reset_provider()
        reset_engine()
        from fastapi.testclient import TestClient
        from app.main import app

        cls.client = TestClient(app)

    def setUp(self):
        os.environ.pop("AUU_UNIVERSE_MOCK", None)
        reset_universe_cache()

    def test_reader_has_no_order_path(self):
        text = Path("app/marketdata/universe.py").read_text(encoding="utf-8").lower()
        self.assertNotIn("sendtransaction", text)
        self.assertNotIn("apikey", text)
        self.assertNotIn("/order", text)
        self.assertNotIn("app.live", text)
        self.assertNotIn("pumpportal", text)

    def _patch_lists(self):
        def fake(url, timeout=None):
            if "tokens/v1" in url:
                return _dex_batch()
            if "created_timestamp" in url and "offset=80" in url:
                return []
            if "created_timestamp" in url:
                return [_curve()]
            if "market_cap" in url:
                return [_grad()]
            if "last_trade" in url:
                return [_curve(), _grad()]
            if "dex/search" in url:
                raise UpstreamError("unavailable", "unused")
            raise UpstreamError("unavailable", url)

        return patch("app.marketdata.universe.get_json", side_effect=fake)

    def test_lists_real_coins_and_skips_mock_pool(self):
        with self._patch_lists():
            hot = self.client.get("/api/v1/universe", params={"tab": "hot", "limit": 50})
            graduating = self.client.get("/api/v1/universe", params={"tab": "graduating"})
            graduated = self.client.get("/api/v1/universe", params={"tab": "graduated"})
            gainers = self.client.get("/api/v1/universe", params={"tab": "gainers"})
        self.assertEqual(hot.status_code, 200)
        data = hot.json()["data"]
        self.assertFalse(data["liveEnabled"])
        self.assertFalse(data["mock_included"])
        symbols = {item["symbol"] for item in data["items"]}
        self.assertIn("CCAT", symbols)
        self.assertIn("GRAD", symbols)
        self.assertNotIn("PUMPDEMO", symbols)
        self.assertNotIn("GRADMOCK", symbols)
        grad = next(item for item in data["items"] if item["symbol"] == "GRAD")
        self.assertAlmostEqual(grad["change_24h"], 0.20)
        self.assertAlmostEqual(grad["change_5m"], 0.015)
        self.assertAlmostEqual(grad["change_1h"], -0.03)
        self.assertEqual(grad["volume_24h_usd"], 9000)
        self.assertEqual(grad["holders"], 1200)
        self.assertEqual(grad["image"], "https://img.example/grad.png")
        self.assertGreater(len(grad["spark"]), 0)
        curve_rows = graduating.json()["data"]["items"]
        self.assertEqual([row["symbol"] for row in curve_rows], ["CCAT"])
        self.assertGreater(curve_rows[0]["progress_pct"], 55)
        self.assertEqual([row["symbol"] for row in graduated.json()["data"]["items"]], ["GRAD"])
        self.assertEqual([row["symbol"] for row in gainers.json()["data"]["items"]], ["GRAD"])
        self.assertTrue(data["movers"])

    def test_dex_fallback_when_pump_lists_fail(self):
        def fake(url, timeout=None):
            if "dex/search" in url:
                return {
                    "pairs": [
                        {
                            "chainId": "solana",
                            "dexId": "pumpfun",
                            "baseToken": {"address": CURVE_MINT, "name": "Fallback", "symbol": "FALL"},
                            "priceUsd": "0.01",
                            "priceNative": "0.0001",
                            "priceChange": {"h24": 5},
                            "volume": {"h24": 100},
                            "liquidity": {"usd": 1000},
                        },
                        {
                            "chainId": "ethereum",
                            "dexId": "uniswap",
                            "baseToken": {"address": "0xabc", "symbol": "ETH"},
                            "priceUsd": "1",
                            "priceNative": "1",
                        },
                    ]
                }
            raise UpstreamError("unavailable", "down")

        with patch("app.marketdata.universe.get_json", side_effect=fake):
            r = self.client.get("/api/v1/universe", params={"tab": "hot"})
        data = r.json()["data"]
        self.assertEqual(r.status_code, 200)
        self.assertFalse(data["liveEnabled"])
        self.assertEqual([item["symbol"] for item in data["items"]], ["FALL"])
        self.assertAlmostEqual(data["items"][0]["change_24h"], 0.05)

    def test_both_sources_fail_soft(self):
        def fake(url, timeout=None):
            raise UpstreamError("unavailable", "down")

        with patch("app.marketdata.universe.get_json", side_effect=fake):
            r = self.client.get("/api/v1/universe")
        data = r.json()["data"]
        self.assertEqual(r.status_code, 200)
        self.assertEqual(data["items"], [])
        self.assertTrue(data["error"])
        self.assertFalse(data["liveEnabled"])

    def test_mock_flag_adds_local_pool(self):
        def fake(url, timeout=None):
            raise UpstreamError("unavailable", "down")

        os.environ["AUU_UNIVERSE_MOCK"] = "1"
        reset_universe_cache()
        try:
            with patch("app.marketdata.universe.get_json", side_effect=fake):
                r = self.client.get("/api/v1/universe", params={"tab": "hot"})
        finally:
            os.environ.pop("AUU_UNIVERSE_MOCK", None)
            reset_universe_cache()
        symbols = {item["symbol"] for item in r.json()["data"]["items"]}
        self.assertIn("PUMPDEMO", symbols)
        self.assertTrue(r.json()["data"]["mock_included"])
