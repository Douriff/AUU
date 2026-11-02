"""空投雷达 RSS ingest: keyword filter, category heuristics, dedup, failure isolation."""
from __future__ import annotations

import os
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import airdrops
from app.airdrops import ingest as ing

NOW = 1791900000000  # ~2026-10-13

RSS_HIT = b"""<?xml version="1.0"?>
<rss version="2.0"><channel><title>F</title>
<item>
  <title>Protocol X announces community airdrop for early users</title>
  <link>https://example.com/airdrop-x</link>
  <guid>g-air-1</guid>
  <pubDate>Mon, 12 Oct 2026 10:00:00 +0000</pubDate>
  <description>Eligibility details for the airdrop claim portal.</description>
</item>
<item>
  <title>Bitcoin ETF sees record inflow</title>
  <link>https://example.com/etf</link>
  <guid>g-etf</guid>
  <pubDate>Mon, 12 Oct 2026 11:00:00 +0000</pubDate>
  <description>Flows only; unrelated markets note.</description>
</item>
<item>
  <title>Pump.fun holder rewards update for meme traders</title>
  <link>https://example.com/pump-hr</link>
  <guid>g-pump</guid>
  <pubDate>Mon, 12 Oct 2026 12:00:00 +0000</pubDate>
  <description>Holder rewards points program changes.</description>
</item>
</channel></rss>"""

RSS_EMPTY_MATCH = b"""<?xml version="1.0"?>
<rss version="2.0"><channel><title>F</title>
<item><title>Weather in New York</title><link>https://example.com/w</link><guid>g-w</guid>
<pubDate>Mon, 12 Oct 2026 10:00:00 +0000</pubDate></item>
</channel></rss>"""


class HeuristicTests(unittest.TestCase):
    def test_keyword_and_category(self):
        self.assertTrue(ing.matches_airdrop("Huge airdrop incoming", ""))
        self.assertTrue(ing.matches_airdrop("空投快讯", ""))
        self.assertTrue(ing.matches_airdrop("New points program", "season 2"))
        self.assertFalse(ing.matches_airdrop("Bitcoin ETF inflow", "macro only"))
        self.assertEqual(ing.guess_category("Pump.fun meme airdrop", ""), "meme")
        self.assertEqual(ing.guess_category("EigenLayer stakedrop season", ""), "major")
        self.assertEqual(ing.guess_category("Random community airdrop", ""), "community")
        self.assertEqual(ing.guess_project("Acme Labs: token airdrop details"), "Acme Labs")
        self.assertEqual(ing.guess_project("Something vague without markers"), "行业资讯")

    def test_exchange_campaigns_match_and_media_stays_strict(self):
        """Titles seen on OKX / Bybit / KuCoin activity feeds must ingest; the same wording on media must not."""
        exchange_keep = [
            "OKX Flash Earn Lite (CT) is Now Live: Subscribe BTC, ETH, OKB or CT to Share 1,000,000 CT",
            "Flash Earn: Hold to Earn and Trade-to-Earn — share 50,000 USDT",
            "Hold to Earn: stake BTC to earn rewards",
            "Trade-to-Earn campaign",
            "Learn-to-Earn week",
            "[Exclusive Launch] Full Stocks Ahead: Share 150,000 USDC",
            "KII Alpha Token Splash— Grab a share of the 2,000,000 KII prize pool .",
            "New Launchpool: Stake to earn TOKEN",
            "Launch pool opens for ABC",
            "Jumpstart sale",
            "Bitget CandyBomb is live",
            "Points season 2",
            "Ritual (RITUAL) Listing Campaign — 2,300,000 RITUAL Giveaway",
            "KuMining Halloween · Keep the Mine Lit — 50,000+ USDT in Rewards",
            "欧易闪赚：持仓赚币，瓜分 50,000 USDT",
            "Bybit Trading Competition: Share 100,000 USDT",
            "Introducing KuCoin Futures Arena: Earn Ongoing Futures Points & Boost Your Multiplier Up to 1.10x",
            "New Listing: Arbitrum (ARB) — Deposit, Trade and Stake ARB to Share a 400,000 USDT Prize Pool!",
            "积分活动开启",
        ]
        exchange_drop = [
            "OKX will launch GRVT/USD for spot trading",
            "OKX to delist DORA, ICX, STORJ spot trading pairs",
            "OKX WebSocket Port 8443 Discontinuation Announcement",
            "Notice: Fee Update for USDT-USD",
            "Bybit AI Now Supports Main Account Operations",
            "Bybit Expands Beyond Crypto With Direct Access to U.S. Stocks & ETFs",
            "New: Bybit Odds Launches U.S. Midterm Election Contracts (Nov 6, 2026)",
            "Bitcoin ETF sees record inflow",
            "UAH P2P Voucher Campaign",
            "Bitget announcement on suspending withdrawals",
            "OKX to list CARDS/USDT (Collector Crypt) for spot trading",
            "Simple Earn flexible rate adjustment",
            "Weather in New York",
        ]
        for title in exchange_keep:
            self.assertTrue(ing.matches_airdrop(title, "", exchange=True), title)
        for title in exchange_drop:
            self.assertFalse(ing.matches_airdrop(title, "", exchange=True), title)
        # media / project blogs do not inherit the exchange campaign vocabulary
        for title in (
            "OKX Flash Earn Lite is Now Live: Hold to Earn and share 50,000 USDT",
            "How traders earn yield on stablecoins",
            "Share 150,000 USDC exclusive launch",
            "Ritual Listing Campaign Giveaway",
            "KuMining 50,000 USDT in Rewards",
            "Introducing KuCoin Futures Arena: Earn Ongoing Futures Points",
            "Bitcoin ETF inflow",
        ):
            self.assertFalse(ing.matches_airdrop(title, "", exchange=False), title)
        for title in ("Protocol X announces community airdrop", "New points program", "空投快讯", "TGE date set", "token claim portal opens"):
            self.assertTrue(ing.matches_airdrop(title, "", exchange=False), title)

    def test_exchange_titles_become_rows_media_copies_do_not(self):
        okx = ing.FEED_SOURCES["okx"]
        media = ing.FEED_SOURCES["coindesk"]
        item = {
            "title": "Flash Earn: Hold to Earn and Trade-to-Earn — share 50,000 USDT",
            "url": "https://www.okx.com/help/flash-earn-hold-to-earn",
            "guid": "g-earn",
            "summary": "Subscribe and share the prize pool.",
            "published_at": NOW,
        }
        row = ing.feed_item_to_row(item, source_id="okx", source_name=okx["name"], now_ms=NOW, meta=okx)
        self.assertIsNotNone(row)
        self.assertEqual(row["kind"], "official")
        self.assertEqual(row["project_name"], "OKX")
        self.assertIn("official", row["tags"].split(","))
        # same headline on a media feed is ordinary promo copy, not an airdrop story
        media_item = dict(item, url="https://www.coindesk.com/markets/flash-earn")
        self.assertIsNone(ing.feed_item_to_row(media_item, source_id="coindesk", source_name=media["name"], now_ms=NOW, meta=media))
        listing = {
            "title": "OKX will launch GRVT/USD for spot trading",
            "url": "https://www.okx.com/help/grvt-usd",
            "guid": "g-list",
            "summary": "Spot trading only.",
            "published_at": NOW,
        }
        self.assertIsNone(ing.feed_item_to_row(listing, source_id="okx", source_name=okx["name"], now_ms=NOW, meta=okx))


class IngestStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.store = airdrops.AirdropStore(root / "airdrops.sqlite")
        airdrops.reset_store(self.store)
        ing.reset_fetcher()

    def tearDown(self):
        ing.reset_fetcher()
        airdrops.reset_store(None)
        self.store.close()
        self.tmp.cleanup()

    def test_tick_filters_dedups_and_keeps_seed(self):
        curated_before = self.store.list(origin="curated", size=100)["total"]
        self.assertGreaterEqual(curated_before, 8)

        def fake_fetch(url: str) -> bytes:
            return RSS_HIT

        fetcher = ing.AirdropFetcher(self.store, fetch=fake_fetch, now_ms=lambda: NOW)
        out = fetcher.tick()
        self.assertTrue(out["coindesk"]["ok"])
        self.assertEqual(out["coindesk"]["matched"], 2)  # airdrop + pump holder rewards
        # second tick (forced: media now have a 240 s politeness floor): dedup
        out2 = fetcher.tick(force=True)
        self.assertEqual(out2["coindesk"]["added"], 0)
        ingested = self.store.list(origin="ingested", size=50)
        self.assertEqual(ingested["total"], 2)
        for it in ingested["items"]:
            self.assertEqual(it["origin"], "ingested")
            self.assertIn("ingested", it["tags"])
            self.assertTrue(it["sourceUrl"].startswith("https://"))
            self.assertTrue(it["sourceName"])
        # curated intact
        self.assertEqual(self.store.list(origin="curated", size=100)["total"], curated_before)
        meme = [i for i in ingested["items"] if i["category"] == "meme"]
        self.assertTrue(meme)

    def test_does_not_overwrite_curated_same_url(self):
        # plant curated-like row then try ingest same URL
        url = "https://example.com/owned"
        self.store._db.execute(
            "INSERT INTO items(id, title, project_name, category, source_name, source_url, summary, chains, "
            "status, published_at, updated_at, external_url, tags, seed_hash, origin) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("curated-owned", "Curated", "P", "community", "Manual", url, "s", "", "active", NOW, NOW, url, "x", "hash", "curated"),
        )
        row = {
            "id": ing.ingested_id("coindesk", url, "g"),
            "title": "Hack attempt airdrop",
            "project_name": "X",
            "category": "community",
            "source_name": "CoinDesk",
            "source_url": url,
            "summary": "airdrop",
            "chains": "",
            "status": "active",
            "published_at": NOW,
            "updated_at": NOW,
            "external_url": url,
            "tags": "ingested",
            "origin": "ingested",
        }
        added, skipped = self.store.upsert_ingested([row])
        self.assertEqual((added, skipped), (0, 1))
        self.assertEqual(self.store.get("curated-owned")["title"], "Curated")

    def test_feed_failure_isolated(self):
        def boom(url: str) -> bytes:
            raise TimeoutError("slow")

        fetcher = ing.AirdropFetcher(self.store, fetch=boom, now_ms=lambda: NOW)
        out = fetcher.tick()
        self.assertFalse(out["coindesk"]["ok"])
        # store still lists curated
        self.assertGreater(self.store.list(size=5)["total"], 0)

    def test_no_match_feed_ok_zero_added(self):
        fetcher = ing.AirdropFetcher(self.store, fetch=lambda u: RSS_EMPTY_MATCH, now_ms=lambda: NOW)
        out = fetcher.tick()
        self.assertTrue(out["coindesk"]["ok"])
        self.assertEqual(out["coindesk"]["matched"], 0)
        self.assertEqual(self.store.list(origin="ingested", size=10)["total"], 0)

    def test_kucoin_activities_keep_giveaway_drop_fee_notice_and_seed(self):
        curated = self.store.list(origin="curated", size=100)["total"]
        payload = """{"code":"200000","data":{"items":[
            {"annId":1,"annTitle":"Ritual (RITUAL) Listing Campaign — 2,300,000 RITUAL Giveaway",
             "annDesc":"Listing campaign giveaway.","annUrl":"https://www.kucoin.com/announcement/ritual-giveaway","cTime":%d},
            {"annId":2,"annTitle":"KuCoin Will Adjust the Spot Trading Fee Rate for Gnoland (GNOT)",
             "annDesc":"Fee update only.","annUrl":"https://www.kucoin.com/announcement/fee-gnot","cTime":%d}
        ]}}""" % (NOW - 3_600_000, NOW - 1_800_000)
        with patch.dict(os.environ, {"AUU_AIRDROPS_FEEDS": "kucoin", "AUU_ROLE": "primary"}):
            out = ing.AirdropFetcher(self.store, fetch=lambda u: payload.encode(), now_ms=lambda: NOW).tick()
        self.assertTrue(out["kucoin"]["ok"], out)
        self.assertEqual(out["kucoin"]["matched"], 1)
        self.assertEqual(out["kucoin"]["added"], 1)
        items = self.store.list(origin="ingested", size=10)["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["projectName"], "KuCoin")
        self.assertEqual(items[0]["verification"], "confirmed")
        self.assertIn("Giveaway", items[0]["title"])
        self.assertEqual(self.store.list(origin="curated", size=100)["total"], curated)
        # a successful scrape with nothing new still records lastIngestAt, while the content date stays put
        listed = self.store.list(size=5)
        self.assertEqual(listed["lastIngestAt"], NOW)
        self.assertGreaterEqual(listed["latestPublishedAt"], items[0]["publishedAt"])

    def test_kucoin_rejects_bad_payload_and_foreign_host(self):
        with self.assertRaises(ValueError):
            ing.parse_kucoin(b'{"code":"400100","msg":"bad","data":{}}', NOW)
        meta = ing.FEED_SOURCES["kucoin"]
        evil = {
            "title": "Some token giveaway",
            "url": "https://phish.example/giveaway",
            "guid": "g",
            "summary": "",
            "published_at": NOW,
        }
        self.assertIsNone(ing.feed_item_to_row(evil, source_id="kucoin", source_name=meta["name"], now_ms=NOW, meta=meta))
        self.assertNotIn("binance", ing.FEED_SOURCES)
        self.assertNotIn("apikey", ing.FEED_SOURCES["kucoin"]["url"].lower())
        self.assertIn("annType=activities", ing.FEED_SOURCES["kucoin"]["url"])


class IngestRouteTests(unittest.TestCase):
    _ENV = (
        "AUU_AUTH", "AUU_ALLOW_SIGNUP", "AUU_USER_STORE", "AUU_USER_JOURNAL_DIR", "AUU_AUTH_RATE_MAX",
        "AUU_LEGACY_PUMP", "AUU_INVITE_CODE", "AUU_ADMIN_USER", "AUU_AIRDROPS_INGEST", "AUU_AIRDROPS_FEEDS",
    )

    def setUp(self):
        from app.auth.accounts import reset_accounts

        self.tmp = tempfile.TemporaryDirectory()
        self._prev = {k: os.environ.get(k) for k in self._ENV}
        root = Path(self.tmp.name)
        os.environ.update({
            "AUU_AUTH": "on", "AUU_ALLOW_SIGNUP": "on",
            "AUU_USER_STORE": str(root / "users.json"),
            "AUU_USER_JOURNAL_DIR": str(root / "journals"),
            "AUU_AUTH_RATE_MAX": "1000", "AUU_LEGACY_PUMP": "off",
            "AUU_AIRDROPS_INGEST": "off",
        })
        os.environ.pop("AUU_INVITE_CODE", None)
        os.environ.pop("AUU_ADMIN_USER", None)
        reset_accounts()
        self.store = airdrops.AirdropStore(root / "airdrops.sqlite")
        # inject one ingested row
        self.store.upsert_ingested([{
            "id": "ing-test-1",
            "title": "Test airdrop news",
            "project_name": "行业资讯",
            "category": "community",
            "source_name": "CoinDesk",
            "source_url": "https://example.com/ing-1",
            "summary": "summary",
            "chains": "",
            "status": "active",
            "published_at": NOW,
            "updated_at": NOW,
            "external_url": "https://example.com/ing-1",
            "tags": "ingested,rss",
            "origin": "ingested",
        }])
        airdrops.reset_store(self.store)
        self._sock = patch.object(socket.socket, "connect", side_effect=AssertionError("network"))
        self._sock.start()
        from fastapi.testclient import TestClient
        from app.main import create_app
        self.c = TestClient(create_app(legacy=False))

    def tearDown(self):
        from app.auth.accounts import reset_accounts
        self._sock.stop()
        airdrops.reset_store(None)
        self.store.close()
        for k, v in self._prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        reset_accounts()
        self.tmp.cleanup()

    def _login(self):
        pw = "paperPass123"
        self.assertEqual(self.c.post("/api/v1/auth/register", json={"name": "vin", "password": pw, "password_confirm": pw}).status_code, 200)

    def test_origin_filter(self):
        self._login()
        r = self.c.get("/api/v1/airdrops?origin=ingested")
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()["data"]
        self.assertGreaterEqual(d["total"], 1)
        self.assertTrue(all(i["origin"] == "ingested" for i in d["items"]))
        self.assertIn("ingestEnabled", d)
        self.assertIn("feeds", d)
        self.assertEqual(self.c.get("/api/v1/airdrops?origin=nope").status_code, 400)


if __name__ == "__main__":
    unittest.main()
