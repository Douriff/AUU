"""行业动态: RSS parsing, coin tagging, store/pagination, failure isolation, login-only route."""
from __future__ import annotations

import json
import os
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from app import news

NOW = 1791185000000  # 2026-10-05 ~15:23 BJ

RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/" xmlns:dc="http://purl.org/dc/elements/1.1/">
<channel><title>Feed</title>
<item>
  <title><![CDATA[Bitcoin ETFs kick off &#8216;Uptober&#8217; with $1.03B inflow]]></title>
  <link><![CDATA[https://example.com/a?utm_source=rss_feed&utm_medium=rss&id=7]]></link>
  <guid isPermaLink="false">a-1</guid>
  <pubDate>Fri, 02 Oct 2026 08:20:08 +0000</pubDate>
  <description><![CDATA[<p style="float:right"><img src="https://x/y.jpg"></p><p>Bitcoin ETFs returned to inflows, while Ether funds saw outflows.</p>]]></description>
  <content:encoded><![CDATA[<p>FULL ARTICLE BODY THAT MUST NOT BE STORED</p>]]></content:encoded>
</item>
<item>
  <title>Bitcoin Cash and Ethereum Classic rally; solar stocks link up</title>
  <link>https://example.com/b</link>
  <pubDate>Sat, 03 Oct 2026 10:00:00 +0000</pubDate>
  <description>Nothing about $SOL here? Actually yes: $SOL and NEAR Protocol.</description>
</item>
<item>
  <title>Future-dated post</title>
  <link>https://example.com/c</link>
  <pubDate>Mon, 05 Oct 2099 10:00:00 +0000</pubDate>
</item>
<item><title>No link item</title></item>
<item><title>Bad scheme</title><link>javascript:alert(1)</link></item>
</channel></rss>"""


class ParseTests(unittest.TestCase):
    def test_parse_keeps_title_summary_link_only(self):
        items = news.parse_feed(RSS, "coindesk", NOW)
        self.assertEqual(len(items), 3)
        a = items[0]
        self.assertEqual(a["title"], "Bitcoin ETFs kick off \u2018Uptober\u2019 with $1.03B inflow")
        self.assertEqual(a["url"], "https://example.com/a?id=7")  # utm_* dropped
        self.assertEqual(a["guid"], "a-1")
        self.assertEqual(a["summary"], "Bitcoin ETFs returned to inflows, while Ether funds saw outflows.")
        self.assertEqual(a["coins"], ["BTC", "ETH"])
        self.assertTrue(a["important"])  # ETF inflow >= $300M
        self.assertNotIn("FULL ARTICLE", repr(items))
        self.assertEqual(a["published_at"], 1790929208000)
        self.assertLessEqual(items[2]["published_at"], NOW + 5 * 60_000)  # future date clamped

    def test_coin_tagging_word_rules(self):
        b = news.parse_feed(RSS, "coindesk", NOW)[1]
        self.assertEqual(b["coins"], ["SOL", "BCH", "ETC", "NEAR"])  # no BTC/ETH from "Bitcoin Cash"/"Ethereum Classic", no LINK from "link"
        self.assertEqual(news.tag_coins("solar panel dot com, uni students near the atom"), [])
        self.assertEqual(news.tag_coins("SOL, LINK and DOT rise; Cardano too"), ["SOL", "ADA", "LINK", "DOT"])
        self.assertEqual(news.tag_coins("Bitcoin and bitcoin cash"), ["BTC", "BCH"])

    def test_summary_is_cut(self):
        s = news.clean_text("<p>" + "word " * 200 + "</p>", news.SUMMARY_MAX)
        self.assertLessEqual(len(s), news.SUMMARY_MAX)
        self.assertTrue(s.endswith("\u2026"))

    def test_dtd_and_entities_refused(self):
        bomb = b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY a "aaaa">]><rss><channel><item><title>&a;</title></item></channel></rss>'
        with self.assertRaises(ValueError):
            news.parse_feed(bomb, "coindesk", NOW)

    def test_atom_entries(self):
        atom = b"""<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Solana upgrade</title><link href="https://e.com/x"/>
        <id>tag:1</id><updated>2026-10-04T01:02:03Z</updated><summary>s</summary></entry></feed>"""
        it = news.parse_feed(atom, "coindesk", NOW)[0]
        self.assertEqual((it["url"], it["guid"], it["coins"], it["important"]), ("https://e.com/x", "tag:1", ["SOL"], False))


# 要闻 rules: (title, expected category or None). Positives = one per major-event category; negatives include the
# real Cointelegraph/CoinDesk titles of 2026-10-05 that the old keyword rule flagged (10 of 30 on Cointelegraph).
IMPORTANT_CASES = [
    # etf: decisions, and flows >= $300M (single day / few days) or >= $1B (week / streak); never quarter recaps
    ("SEC approves first spot Solana ETFs", "etf"),
    ("Bitcoin ETFs draw record $1.4B daily inflow", "etf"),
    ("Bitcoin ETFs add $347M as BTC falls below $84K after topping $87K", "etf"),
    ("Bitcoin ETF outflows accelerate as investors pull $449M in three days", "etf"),
    ("Bitcoin ETFs draw $2.4B in biggest inflow week since October 2025", "etf"),
    ("Bitcoin ETFs kick off \u2018Uptober\u2019 with $103M inflow", None),
    ("Bitcoin ETFs draw $6.3B in Q3 as BTC price rises nearly 43%", None),
    ("US crypto ETF inflows top $600M for the week", None),
    ("Fidelity files with SEC to add staking to Ethereum ETF", None),
    ("BNB Chain crosses $1B in tokenized stocks, ETFs as market hits $3.7B", None),
    # regulation / enforcement: the regulator / court acts; laws signed or passed
    ("DOJ charges Tornado Cash developers with money laundering", "regulation"),
    ("SEC sues Binance and CZ over securities violations", "regulation"),
    ("Senate passes GENIUS Act in 68-30 vote", "regulation"),
    ("Trump signs executive order on strategic Bitcoin reserve", "regulation"),
    ("Do Kwon sentenced in New York to 15 years", "regulation"),  # passive enforcement form
    ("US SEC follows CFTC in staff guidance for crypto", "regulation"),
    ("Community banks sue OCC over trust bank charters of crypto firms", None),
    ("Bank group sues U.S. regulator over granting crypto trust charters", None),
    ("Japan adds Garantex to list of Russia sanctions over Ukraine war", None),
    ("European crypto users have \u2018more faith\u2019 in regulated firms under MiCA: Bitpanda co-CEO", None),
    ("Trump\u2019s potential AI czar, Jay Clayton, helped pioneer the SEC\u2019s crypto crackdown", None),
    ("New York, Wyoming regulators sign pact to coordinate crypto oversight", None),
    # hack / outage: top exchange, or >= $50M; follow-ups are not a new incident
    ("Bybit hacked for $1.5 billion in largest crypto heist ever", "hack"),
    ("Hackers drain $120M from Balancer pools", "hack"),
    ("Coinbase suffers outage as trading volume spikes", "hack"),
    ("Binance halts withdrawals amid network congestion", "hack"),
    ("NEAR Intents recovers entire stolen $3.8M after ultimatum to exploiter", None),
    ("Aave founder says V3 unaffected after third-party adapter exploit drains $305K", None),
    ("SlowMist traces Bitget hack activity to Aug. 31 zero-day exploit", None),
    # macro
    ("Fed cuts rates by 25 basis points, signals more easing", "macro"),
    ("FOMC holds rates steady as Powell warns on inflation", "macro"),
    ("Bank of Japan raises rates to highest since 2008", "macro"),
    ("US CPI rises 0.4% in August, hotter than expected", "macro"),
    ("U.S. added just 29,000 jobs in September, with unemployment rate rising to 4.2%", "macro"),
    ("Bitcoin briefly hits $87K as weak US jobs data sends bond yields lower", None),
    # BTC/ETH/SOL big moves
    ("Bitcoin crashes below $100K as traders flee risk", "move"),
    ("Bitcoin hits new all-time high above $126,000", "move"),
    ("Ether drops 12% as liquidations mount", "move"),
    ("Bitcoin falls 8% in a day", "move"),
    ("Solana slides 6% after network hiccup", None),  # SOL threshold is 10 %
    ("Crypto liquidations top $2 billion as Bitcoin slides", "move"),
    ("Bitcoin reaches for $87K as short liquidations top $120M", None),
    ("Bitcoin zooms toward $87,000, nearly setting an eight-month high, then reverses", None),
    ("Once a $2 billion Ethereum layer-2, Blast is shutting down after assets plunge 98%", None),
    ("NEAR jumps nearly 80% in a week as Intents volume nears $30B", None),
    ("Bitcoin falls below $84K as 10-year Treasury yield hits 19-year high", None),
    # top institutions buying / selling
    ("Strategy buys 1,665 Bitcoin for $143M as BTC stack hits 847,666", "whale"),
    ("BlackRock buys $500M of Ether for its fund", "whale"),
    ("US government moves 10,000 BTC to Coinbase", "whale"),
    ("Strategy raises $334M through stock sales but buys no Bitcoin", None),
    ("Bitmine adds $19.6M in ETH, repurchases 4.5M shares", None),
    # roundups / opinion / speculation are never 要闻
    ("Former SEC boss made AI Czar, Bitcoin may hit $600K this cycle: Hodler\u2019s Digest", None),
    ("Here\u2019s what happened in crypto today", None),
    ("Bitcoin price could crash 30%, analyst says", None),
    ("Will the SEC approve a Solana ETF?", None),
    ("Zcash activates NU7 on testnet ahead of November mainnet target", None),
    # case-sensitive acronyms: the pronoun "us" / "fed up" are not the US / the Fed
    ("Tell us why Saylor adds $200M of bitcoin", None),
    ("Traders fed up as rates stay high and Bitcoin chops", None),
    ("Upbit hacked for $30M in Solana hot wallet breach: Report", "hack"),
    ("US sells $250M of seized Bitcoin", "whale"),
]


class ImportantRuleTests(unittest.TestCase):
    def test_categories_and_real_false_positives(self):
        bad = [(t, exp, news.importance(t)) for t, exp in IMPORTANT_CASES if news.importance(t) != exp]
        self.assertEqual(bad, [])
        for t, exp in IMPORTANT_CASES:
            self.assertEqual(news.is_important(t), exp is not None, t)

    def test_month_may_is_not_speculation(self):
        self.assertEqual(news.importance("SEC approves spot Ether ETFs in May"), "etf")
        self.assertIsNone(news.importance("SEC may approve spot Ether ETFs"))

    def test_usd_amounts(self):
        self.assertEqual(news.usd_amounts("$103M, $1.5 billion, $87,000 and $305K"), [103e6, 1.5e9, 87000.0, 305e3])
        self.assertEqual(news.usd_amounts("no money here"), [])

    def test_ratio_on_the_2026_10_05_feed_snapshot(self):
        # Replay of the 55 headlines stored on the server at the first fetch (25 CoinDesk + 30 Cointelegraph).
        snap = json.loads((Path(__file__).parent / "fixtures" / "news_titles_20261005.json").read_text(encoding="utf-8"))["items"]
        self.assertEqual(len(snap), 55)
        old = [r for r in snap if r["old_important"]]
        self.assertEqual((len(old), sum(r["source"] == "cointelegraph" for r in old)), (12, 10))  # v1: 21.8 %, Cointelegraph 10/30
        new = [(r["source"], r["title"], news.importance(r["title"])) for r in snap if news.is_important(r["title"])]
        # quiet news day: only the US September jobs report qualifies (1/55 = 1.8 %; Cointelegraph 0/30)
        self.assertEqual(new, [("coindesk", "U.S. added just 29,000 jobs in September, with unemployment rate rising to 4.2%", "macro")])
        self.assertLessEqual(len(new) / len(snap), 0.15)

    def test_ratio_on_a_busier_sample(self):
        # 145 more Cointelegraph headlines (tag feeds, weeks of ETF / hack / regulation news): ~9 % flagged.
        snap = json.loads((Path(__file__).parent / "fixtures" / "news_titles_extra_20261005.json").read_text(encoding="utf-8"))["items"]
        cats = [news.importance(r["title"]) for r in snap]
        flagged = [c for c in cats if c]
        self.assertEqual(len(snap), 145)
        self.assertEqual(len(flagged), 13)
        self.assertEqual({c: flagged.count(c) for c in set(flagged)}, {"etf": 9, "whale": 2, "regulation": 2})
        self.assertTrue(0.05 <= len(flagged) / len(snap) <= 0.15)

    def test_retag_on_open_updates_old_flags(self):
        with tempfile.TemporaryDirectory() as d:
            st = news.NewsStore(Path(d) / "news.sqlite")
            st.add([{"source": "cointelegraph", "guid": g, "url": f"https://e.com/{g}", "title": t, "summary": "", "published_at": NOW,
                     "coins": [], "important": True} for g, t in (("1", "Zcash activates NU7 on testnet ahead of November mainnet target"),
                                                                 ("2", "Bybit hacked for $1.5 billion in largest crypto heist ever"))], NOW)
            self.assertEqual(st.page(important=True)["total"], 2)  # flags as written by the old rule
            st.close()
            st = news.NewsStore(Path(d) / "news.sqlite")  # opening re-applies the current rules
            self.assertEqual([i["title"][:5] for i in st.page(important=True)["items"]], ["Bybit"])
            self.assertEqual(st.retag(), 0)  # idempotent
            st.close()


class StoreFetcherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = news.NewsStore(Path(self.tmp.name) / "news.sqlite")
        self._env = patch.dict(os.environ, {"AUU_NEWS_SOURCES": ""})
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self.store.close()
        self.tmp.cleanup()

    def test_dedupe_pagination_filters_prune(self):
        items = news.parse_feed(RSS, "coindesk", NOW)
        self.assertEqual(self.store.add(items, NOW), 3)
        self.assertEqual(self.store.add(items, NOW + 1), 0)  # same guid -> ignored
        p1 = self.store.page(page=1, size=2)
        self.assertEqual((p1["total"], p1["pages"], len(p1["items"])), (3, 2, 2))
        self.assertEqual(p1["items"][0]["title"], "Future-dated post")  # newest first
        self.assertEqual(len(self.store.page(page=2, size=2)["items"]), 1)
        self.assertEqual([i["url"] for i in self.store.page(coin="ETH")["items"]], ["https://example.com/a?id=7"])
        self.assertEqual(self.store.page(focus=True)["total"], 2)
        self.assertEqual(self.store.page(important=True)["total"], 1)
        self.assertEqual(self.store.page(source="cointelegraph")["total"], 0)
        self.assertEqual(self.store.prune(NOW + 400 * 86_400_000), 3)
        self.assertEqual(self.store.page()["total"], 0)

    def test_one_source_down_does_not_stop_the_other_and_tick_never_raises(self):
        def fetch(url):
            if "coindesk" in url:
                raise URLError("down")
            return RSS

        f = news.NewsFetcher(self.store, fetch=fetch, now_ms=lambda: NOW)
        out = f.tick()
        self.assertFalse(out["coindesk"]["ok"])
        self.assertTrue(out["cointelegraph"]["ok"])
        self.assertEqual(out["cointelegraph"]["added"], 3)
        src = {s["id"]: s for s in self.store.page()["sources"]}
        self.assertEqual((src["coindesk"]["ok"], src["coindesk"]["fails"], src["coindesk"]["error"]), (False, 1, "network"))
        self.assertTrue(src["cointelegraph"]["ok"])

        def broken(url):
            if "coindesk" in url:
                raise HTTPError(url, 503, "x", {}, None)
            return b"<rss><channel><item>"  # truncated XML

        out = news.NewsFetcher(self.store, fetch=broken, now_ms=lambda: NOW + 1).tick()
        self.assertEqual(out["coindesk"]["error"], "http 503")
        self.assertTrue(out["cointelegraph"]["error"].startswith("parse"))
        self.assertEqual(self.store.page()["total"], 3)  # earlier items kept

    def test_disk_failure_is_swallowed(self):
        f = news.NewsFetcher(self.store, fetch=lambda u: RSS, now_ms=lambda: NOW)
        with patch.object(self.store, "add", side_effect=OSError("disk full")), patch.object(self.store, "log", side_effect=OSError("disk full")):
            out = f.tick()
        self.assertFalse(out["coindesk"]["ok"])

    def test_enabled_and_interval(self):
        with patch.dict(os.environ, {"AUU_TEST": "1"}):
            os.environ.pop("AUU_NEWS", None)
            self.assertFalse(news.news_enabled())  # never fetches the internet under the test runner by default
        with patch.dict(os.environ, {"AUU_NEWS": "on"}):
            self.assertTrue(news.news_enabled())
        with patch.dict(os.environ, {"AUU_NEWS": "off", "AUU_TEST": "0"}):
            self.assertFalse(news.news_enabled())
        with patch.dict(os.environ, {"AUU_NEWS_INTERVAL_SEC": "5"}):
            self.assertEqual(news.interval_sec(), 300)
        with patch.dict(os.environ, {"AUU_NEWS_INTERVAL_SEC": ""}):
            self.assertEqual(news.interval_sec(), 720)
        with patch.dict(os.environ, {"AUU_NEWS_SOURCES": "cointelegraph,bogus"}):
            self.assertEqual(news.enabled_sources(), ["cointelegraph"])


class RouteTests(unittest.TestCase):
    _ENV = ("AUU_AUTH", "AUU_ALLOW_SIGNUP", "AUU_USER_STORE", "AUU_USER_JOURNAL_DIR", "AUU_AUTH_RATE_MAX", "AUU_LEGACY_PUMP",
            "AUU_INVITE_CODE", "AUU_ADMIN_USER", "AUU_NEWS_SOURCES")

    def setUp(self):
        from app.auth.accounts import reset_accounts

        self.tmp = tempfile.TemporaryDirectory()
        self._prev = {k: os.environ.get(k) for k in self._ENV}
        root = Path(self.tmp.name)
        os.environ.update({"AUU_AUTH": "on", "AUU_ALLOW_SIGNUP": "on", "AUU_USER_STORE": str(root / "users.json"),
                           "AUU_USER_JOURNAL_DIR": str(root / "journals"), "AUU_AUTH_RATE_MAX": "1000", "AUU_LEGACY_PUMP": "off",
                           "AUU_NEWS_SOURCES": ""})
        os.environ.pop("AUU_INVITE_CODE", None)
        os.environ.pop("AUU_ADMIN_USER", None)
        reset_accounts()
        self.store = news.NewsStore(root / "news.sqlite")
        self.store.add(news.parse_feed(RSS, "cointelegraph", NOW), NOW)
        news.reset_store(self.store)
        self._sock = patch.object(socket.socket, "connect", side_effect=AssertionError("network access in test"))
        self._sock.start()
        from fastapi.testclient import TestClient

        from app.main import create_app

        self.c = TestClient(create_app(legacy=False))

    def tearDown(self):
        from app.auth.accounts import reset_accounts

        self._sock.stop()
        news.reset_store(None)
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

    def test_login_required_then_paginated(self):
        self.assertEqual(self.c.get("/api/v1/news").status_code, 401)
        self._login()
        r = self.c.get("/api/v1/news?size=2")
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()["data"]
        self.assertEqual((d["total"], d["pages"], len(d["items"])), (3, 2, 2))
        it = d["items"][1]
        self.assertEqual(it["sourceName"], "Cointelegraph")
        self.assertEqual(set(it), {"id", "source", "sourceName", "title", "summary", "url", "publishedAt", "coins", "important"})
        self.assertIn("版权归原作者", d["note"])
        self.assertEqual(self.c.get("/api/v1/news?coin=eth").json()["data"]["total"], 1)
        self.assertEqual(self.c.get("/api/v1/news?focus=true").json()["data"]["total"], 2)
        self.assertEqual(self.c.get("/api/v1/news?coin=PEPE").status_code, 400)
        self.assertEqual(self.c.get("/api/v1/news?size=500").status_code, 422)
        self.assertEqual(self.c.get("/api/v1/news?source=evil").status_code, 400)

    def test_missing_store_reads_empty_and_creates_nothing(self):
        empty = Path(self.tmp.name) / "empty"
        empty.mkdir()
        news.reset_store(None)
        self._login()
        with patch.object(news, "data_dir", lambda: empty):
            d = self.c.get("/api/v1/news").json()["data"]
        self.assertEqual((d["total"], d["items"]), (0, []))
        self.assertFalse((empty / news.LEDGER_NAME).exists())
        self.assertEqual([s["id"] for s in d["sources"]], ["coindesk", "cointelegraph"])

    def test_store_error_is_a_503_not_a_crash(self):
        self._login()
        with patch.object(self.store, "page", side_effect=RuntimeError("boom")):
            r = self.c.get("/api/v1/news")
        self.assertEqual(r.status_code, 503)
        self.assertEqual(self.c.get("/api/v1/status").status_code, 200)


if __name__ == "__main__":
    unittest.main()
