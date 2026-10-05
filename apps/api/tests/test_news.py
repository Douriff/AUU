"""行业动态: RSS parsing, coin tagging, store/pagination, failure isolation, login-only route."""
from __future__ import annotations

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
  <title><![CDATA[Bitcoin ETFs kick off &#8216;Uptober&#8217; with $103M inflow]]></title>
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
        self.assertEqual(a["title"], "Bitcoin ETFs kick off \u2018Uptober\u2019 with $103M inflow")
        self.assertEqual(a["url"], "https://example.com/a?id=7")  # utm_* dropped
        self.assertEqual(a["guid"], "a-1")
        self.assertEqual(a["summary"], "Bitcoin ETFs returned to inflows, while Ether funds saw outflows.")
        self.assertEqual(a["coins"], ["BTC", "ETH"])
        self.assertTrue(a["important"])  # ETF
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
        self.assertEqual((it["url"], it["guid"], it["coins"], it["important"]), ("https://e.com/x", "tag:1", ["SOL"], True))


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
