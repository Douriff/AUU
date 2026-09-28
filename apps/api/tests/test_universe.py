"""Universe search, DexScreener fallback, and per-venue CEX degradation."""
from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import patch

from app.marketdata.cex import reset_cex_cache
from app.marketdata.fetch import UpstreamError
from app.marketdata.pump_search import reset_search_cache
from app.paper.broker import reset_paper_broker
from app.paper.decision_log import reset_decision_log
from app.paper.ledger import get_paper_journal, reset_paper_ledger
from app.providers import reset_provider
from app.risk.gate import reset_risk_gate
from app.strategies.pump_paper_v1 import reset_engine

EXT_MINT = "ExtMint11111111111111111111111111111111"
CURVE_MINT = "CurveMint111111111111111111111111111111"


def _curve_coin() -> dict:
    return {
        "mint": CURVE_MINT,
        "name": "Alpha Cat",
        "symbol": "ACAT",
        "image_uri": "https://img.example/acat.png",
        "virtual_sol_reserves": 30_000_000_000,
        "virtual_token_reserves": 1_073_000_000_000_000,
        "real_sol_reserves": 1_000_000_000,
        "real_token_reserves": 400_000_000_000_000,
        "total_supply": 1_000_000_000_000_000,
        "usd_market_cap": 40_000,
        "complete": False,
    }


def _dex_pairs() -> dict:
    return {
        "pairs": [
            {
                "chainId": "ethereum",
                "dexId": "uniswap",
                "baseToken": {"address": "0xabc", "name": "Eth", "symbol": "ETH"},
                "priceUsd": "1",
                "priceNative": "1",
            },
            {
                "chainId": "solana",
                "dexId": "orca",
                "baseToken": {"address": "OrcaMint111111111111111111111111111111", "symbol": "ORC", "name": "Orca"},
                "priceUsd": "1",
                "priceNative": "0.01",
                "liquidity": {"usd": 999999},
            },
            {
                "chainId": "solana",
                "dexId": "raydium",
                "baseToken": {"address": EXT_MINT, "name": "External", "symbol": "EXT", "imageUrl": "https://img.example/ext.png"},
                "priceUsd": "0.02",
                "priceNative": "0.0001",
                "volume": {"h24": 2500},
                "priceChange": {"h24": 4.5},
                "marketCap": 80000,
                "liquidity": {"usd": 200000},
                "info": {"imageUrl": "https://img.example/ext.png"},
            },
            {
                "chainId": "solana",
                "dexId": "pumpswap",
                "baseToken": {"address": "PumpSwapMint11111111111111111111111111", "name": "Swap", "symbol": "PSW"},
                "priceUsd": "0.01",
                "priceNative": "0.00005",
                "liquidity": {"usd": 50000},
                "priceChange": {"h24": -2},
                "volume": {"h24": 100},
            },
        ]
    }


class UniverseApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["DATA_PROVIDER"] = "pumpfun_paper"
        os.environ["PUMP_PAPER_LOOP"] = "0"
        os.environ["PUMPFUN_DISCOVERY"] = "off"
        os.environ.pop("PUMPFUN_WATCH_MINTS", None)
        reset_provider()
        reset_engine()
        reset_risk_gate()
        reset_paper_broker()
        reset_paper_ledger()
        reset_decision_log()
        reset_search_cache()
        reset_cex_cache()
        from fastapi.testclient import TestClient
        from app.main import app

        cls.client = TestClient(app)

    @classmethod
    def tearDownClass(cls):
        os.environ["DATA_PROVIDER"] = "mock"
        os.environ["PUMP_PAPER_LOOP"] = "1"
        reset_provider()
        reset_engine()
        reset_risk_gate()
        reset_paper_broker()
        reset_paper_ledger()

    def setUp(self):
        reset_paper_ledger()
        reset_risk_gate()
        reset_paper_broker()
        reset_engine()
        reset_decision_log()
        reset_search_cache()
        reset_cex_cache()

    def test_readers_have_no_order_path(self):
        for name in ("pump_search.py", "cex.py", "fetch.py"):
            text = (Path("app/marketdata") / name).read_text(encoding="utf-8").lower()
            self.assertNotIn("sendtransaction", text)
            self.assertNotIn("apikey", text)
            self.assertNotIn("/order", text)
            self.assertNotIn("app.live", text)

    def test_pump_search_returns_curve_fields(self):
        def fake(url, timeout=None):
            self.assertIn("coins/search", url)
            return [_curve_coin()]

        with patch("app.marketdata.pump_search.get_json", side_effect=fake):
            r = self.client.get("/api/v1/search", params={"q": "acat"})
        self.assertEqual(r.status_code, 200)
        data = r.json()["data"]
        self.assertFalse(data["liveEnabled"])
        self.assertIsNone(data["error"])
        self.assertEqual(len(data["items"]), 1)
        item = data["items"][0]
        self.assertEqual(item["mint"], CURVE_MINT)
        self.assertEqual(item["symbol"], "ACAT")
        self.assertEqual(item["image"], "https://img.example/acat.png")
        self.assertGreater(item["price_sol"], 0)
        self.assertGreater(item["price_usd"], 0)
        self.assertIsNotNone(item["market_cap_usd"])
        self.assertFalse(item["graduated"])
        self.assertEqual(item["venue"], "曲线")
        self.assertGreater(item["progress_pct"], 0)

    def test_fallback_filters_solana_pump_and_raydium(self):
        def fake(url, timeout=None):
            if "dex/search" in url:
                return _dex_pairs()
            raise UpstreamError("unavailable", "timeout")

        with patch("app.marketdata.pump_search.get_json", side_effect=fake):
            r = self.client.get("/api/v1/search", params={"q": "ext"})
        data = r.json()["data"]
        self.assertFalse(data["liveEnabled"])
        mints = [item["mint"] for item in data["items"]]
        self.assertEqual(mints, [EXT_MINT, "PumpSwapMint11111111111111111111111111"])
        by_mint = {item["mint"]: item for item in data["items"]}
        self.assertEqual(by_mint[EXT_MINT]["venue"], "Raydium")
        self.assertTrue(by_mint[EXT_MINT]["graduated"])
        self.assertAlmostEqual(by_mint[EXT_MINT]["change_24h"], 0.045)
        self.assertEqual(by_mint[EXT_MINT]["volume_24h_usd"], 2500)
        self.assertEqual(by_mint["PumpSwapMint11111111111111111111111111"]["venue"], "PumpSwap")

    def test_both_sources_fail_soft(self):
        def fake(url, timeout=None):
            raise UpstreamError("unavailable", "timeout")

        with patch("app.marketdata.pump_search.get_json", side_effect=fake):
            r = self.client.get("/api/v1/search", params={"q": "missing"})
        data = r.json()["data"]
        self.assertEqual(r.status_code, 200)
        self.assertEqual(data["items"], [])
        self.assertTrue(data["error"])
        self.assertFalse(data["liveEnabled"])

    def test_graduated_mint_paper_order_uses_estimate(self):
        def fake(url, timeout=None):
            if url.rstrip("/").endswith(f"/coins/{EXT_MINT}"):
                return {
                    "mint": EXT_MINT,
                    "name": "External",
                    "symbol": "EXT",
                    "complete": True,
                    "virtual_sol_reserves": 0,
                    "virtual_token_reserves": 0,
                }
            if "solana/" in url or "tokens/" in url:
                return _dex_pairs()["pairs"]
            if "candles" in url:
                raise UpstreamError("unavailable", "timeout")
            raise UpstreamError("unavailable", "timeout")

        before = self.client.get("/api/v1/strategy/pump-paper-v1").json()["data"]
        with patch("app.marketdata.pump_search.get_json", side_effect=fake):
            preview = self.client.get(
                "/api/v1/trade/preview",
                params={"mint": EXT_MINT, "side": "buy", "notional_sol": 0.1},
            )
            self.assertEqual(preview.status_code, 200, preview.text)
            body = preview.json()["data"]
            self.assertEqual(body["impact_kind"], "estimate")
            self.assertEqual(body["impact_label"], "估算")
            self.assertFalse(body["blocked"])
            self.assertFalse(body["liveEnabled"])
            self.assertLess(body["impact_bps"], 150)
            order = self.client.post(
                "/api/v1/trade/orders",
                json={"mint": EXT_MINT, "side": "buy", "notional_sol": 0.1},
            )
        self.assertEqual(order.status_code, 200, order.text)
        data = order.json()["data"]
        self.assertTrue(data["submitted"])
        self.assertFalse(data["liveEnabled"])
        self.assertEqual(data["source"], "manual")
        lots = get_paper_journal().lots[data["symbol"]]
        self.assertIn("source=manual", lots[0].tag)
        after = self.client.get("/api/v1/strategy/pump-paper-v1").json()["data"]
        self.assertEqual(after["params"], before["params"])
        self.assertFalse(after["auto_paper_orders"])
        self.assertFalse(self.client.get("/api/v1/live/status").json()["data"].get("liveEnabled", False))

        reset_search_cache()
        thin = dict(_dex_pairs()["pairs"][2])
        thin["liquidity"] = {"usd": 100}

        def thin_fake(url, timeout=None):
            if url.rstrip("/").endswith(f"/coins/{EXT_MINT}"):
                return {"mint": EXT_MINT, "name": "External", "symbol": "EXT", "complete": True}
            if "solana/" in url or "/tokens/" in url:
                return [thin]
            raise UpstreamError("unavailable", "timeout")

        with patch("app.marketdata.pump_search.get_json", side_effect=thin_fake):
            blocked = self.client.post(
                "/api/v1/trade/orders",
                json={"mint": EXT_MINT, "side": "buy", "notional_sol": 0.1},
            )
        self.assertEqual(blocked.status_code, 200)
        self.assertFalse(blocked.json()["data"]["submitted"])
        self.assertIn("估算冲击", blocked.json()["data"]["reject"]["notes"])

    def test_venue_timeout_keeps_the_others(self):
        def fake(url, timeout=None):
            if "binance" in url:
                raise UpstreamError("unavailable", "timeout")
            if "okx.com" in url:
                last = "150" if "SOL" in url else "100" if "BTC" in url else "10"
                opened = "140" if "SOL" in url else "100"
                return {"data": [{"last": last, "open24h": opened, "volCcy24h": "500000"}]}
            if "bybit" in url:
                last = "151.5" if "SOL" in url else "100"
                return {"result": {"list": [{"lastPrice": last, "price24hPcnt": "0.01", "turnover24h": "800000"}]}}
            if "coinbase" in url and url.endswith("/ticker"):
                return {"price": "150.2"}
            if "coinbase" in url and url.endswith("/stats"):
                return {"open": "149", "volume": "1000"}
            raise UpstreamError("not_found", "404")

        with patch("app.marketdata.cex.get_json", side_effect=fake):
            r = self.client.get("/api/v1/majors")
        self.assertEqual(r.status_code, 200)
        data = r.json()["data"]
        self.assertFalse(data["liveEnabled"])
        self.assertEqual(data["bases"], ["SOL", "BTC", "ETH"])
        health = {row["id"]: row for row in data["venues"]}
        self.assertEqual(health["binance"]["status"], "unavailable")
        self.assertEqual(health["binance"]["status_label"], "不可用")
        self.assertEqual(health["okx"]["status"], "ok")
        self.assertEqual(health["bybit"]["status"], "ok")
        self.assertEqual(health["coinbase"]["status"], "ok")
        sol = next(row for row in data["rows"] if row["base"] == "SOL")
        self.assertEqual(sol["quotes"]["binance"]["status"], "unavailable")
        self.assertEqual(sol["quotes"]["okx"]["last"], 150.0)
        self.assertAlmostEqual(sol["quotes"]["bybit"]["last"], 151.5)
        self.assertIsNotNone(sol["spread_bps"])
        self.assertGreater(sol["spread_bps"], 0)
        with patch("app.marketdata.cex.get_json", side_effect=fake):
            cmp = self.client.get("/api/v1/majors/compare", params={"base": "SOL", "onchain_usd": 140})
        compared = cmp.json()["data"]
        self.assertTrue(compared["listed"])
        self.assertFalse(compared["liveEnabled"])
        binance = next(row for row in compared["venues"] if row["id"] == "binance")
        self.assertEqual(binance["status_label"], "不可用")
        okx = next(row for row in compared["venues"] if row["id"] == "okx")
        self.assertAlmostEqual(okx["vs_onchain"], (150 - 140) / 140)


if __name__ == "__main__":
    unittest.main()
