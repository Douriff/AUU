"""空投雷达 查看原文: never link to an RSS feed endpoint."""
from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from app import airdrops
from app.airdrops import ingest as ing

NOW = 1791900000000
FEED = "https://www.coindesk.com/arc/outboundfeeds/rss"

RSS = b"""<?xml version="1.0"?>
<rss version="2.0"><channel><title>CoinDesk</title><link>https://www.coindesk.com/</link>
<item><title>Protocol Y airdrop opens claim portal</title>
<link>https://www.coindesk.com/tech/2026/10/13/protocol-y-airdrop</link>
<guid isPermaLink="false">abc-123</guid><pubDate>Mon, 12 Oct 2026 10:00:00 +0000</pubDate>
<description>airdrop</description></item>
<item><title>Protocol Z airdrop announced</title>
<link>https://www.coindesk.com/arc/outboundfeeds/rss</link>
<guid isPermaLink="true">https://www.coindesk.com/markets/2026/10/13/protocol-z-airdrop</guid>
<pubDate>Mon, 12 Oct 2026 11:00:00 +0000</pubDate><description>airdrop</description></item>
</channel></rss>"""


class LinkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "a.sqlite"

    def tearDown(self):
        self.tmp.cleanup()

    def test_is_feed_url(self):
        self.assertTrue(airdrops.is_feed_url(FEED))
        self.assertTrue(airdrops.is_feed_url("https://cointelegraph.com/rss"))
        self.assertFalse(airdrops.is_feed_url("https://www.coindesk.com/tech/2026/10/13/x"))

    def test_ingest_uses_article_link_not_feed(self):
        st = airdrops.AirdropStore(self.db)
        f = ing.AirdropFetcher(st, fetch=lambda u: RSS, now_ms=lambda: NOW)
        with unittest.mock.patch.object(ing, "enabled_feeds", return_value=["coindesk"]):
            f.tick()
        rows = [r for r in st._db.execute("SELECT source_url, external_url FROM items WHERE origin='ingested'")]
        urls = sorted(r["external_url"] for r in rows)
        self.assertEqual(urls, [
            "https://www.coindesk.com/markets/2026/10/13/protocol-z-airdrop",
            "https://www.coindesk.com/tech/2026/10/13/protocol-y-airdrop",
        ])
        for r in rows:
            self.assertFalse(airdrops.is_feed_url(r["source_url"]))

    def test_seed_has_no_feed_urls(self):
        for i in json.loads(airdrops.SEED_PATH.read_text()):
            for k in ("source_url", "external_url"):
                self.assertFalse(airdrops.is_feed_url(i.get(k)), (i["id"], k))

    def test_repair_existing_feed_rows(self):
        st = airdrops.AirdropStore(self.db)
        ins = ("INSERT INTO items(id,title,project_name,category,source_name,source_url,published_at,updated_at,"
               "external_url,origin) VALUES (?,?,?,?,?,?,?,?,?,?)")
        st._db.execute(ins, ("c1", "t", "p", "community", "CoinDesk", "https://www.coindesk.com/", 1, 1, FEED, "curated"))
        st._db.execute(ins, ("i1", "t", "p", "community", "CoinDesk", FEED, 1, 1, FEED, "ingested"))
        st._db.execute(ins, ("i2", "t", "p", "community", "CoinDesk", FEED, 1, 1, "https://www.coindesk.com/a/b", "ingested"))
        st.close()
        st = airdrops.AirdropStore(self.db)
        get = lambda i: st._db.execute("SELECT source_url, external_url FROM items WHERE id=?", (i,)).fetchone()
        self.assertEqual(tuple(get("c1")), ("https://www.coindesk.com/", "https://www.coindesk.com/"))
        self.assertIsNone(get("i1"))
        self.assertEqual(tuple(get("i2")), ("https://www.coindesk.com/a/b", "https://www.coindesk.com/a/b"))


import unittest.mock  # noqa: E402

if __name__ == "__main__":
    unittest.main()
