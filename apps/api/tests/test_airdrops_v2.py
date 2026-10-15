"""空投雷达 v2: official layer (OKX / Bybit APIs + project RSS), dedup/merge, 已确认/未核实, scam filter, standby."""
from __future__ import annotations

import json
import os
import socket
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import airdrops
from app.airdrops import ingest as ing
from app.airdrops import quality as q

FIX = Path(__file__).resolve().parent / "fixtures"
NOW = 1792062900000  # 2026-10-15 ~20:00 CST; the real-capture fixtures were taken that day
DAY = 86_400_000


def _row(**kw):
    base = {
        "id": "x", "title": "t", "project_name": "P", "category": "community", "source_name": "S",
        "source_url": "https://example.com/a", "summary": "", "chains": "", "status": "active",
        "published_at": NOW, "updated_at": NOW, "external_url": "", "tags": "ingested", "origin": "ingested",
        "source_id": "s1", "kind": "media",
    }
    base.update(kw)
    if not base["external_url"]:
        base["external_url"] = base["source_url"]
    return base


class ScamFilterTests(unittest.TestCase):
    def test_required_phrases_are_blocked(self):
        for text in (
            "保证收益的空投，先到先得", "本活动保证盈利", "参与前请先转账 100 USDT 激活资格", "先打款再领取",
            "Send first, receive double", "send 0.1 ETH first to unlock the drop", "Guaranteed airdrop allocation for all",
            "guaranteed returns", "Risk-free airdrop", "double your ETH", "稳赚不赔", "零风险空投",
        ):
            self.assertIsNotNone(q.blocked_reason(text), text)

    def test_benign_text_is_kept(self):
        for text in (
            "Arbitrum announces community airdrop details", "不构成空投承诺或收益保证", "Send your first transaction on Base",
            "官方空投领取页面说明", "Points program season 2", "OKX Jumpstart: new token",
        ):
            self.assertIsNone(q.blocked_reason(text), text)

    def test_seed_has_no_blocked_rows(self):
        for it in airdrops.load_seed():
            self.assertIsNone(q.blocked_reason(it["title"], it["summary"]), it["id"])

    def test_normalize_seed_drops_blocked(self):
        raw = {"id": "bad", "title": "保证收益空投", "project_name": "p", "category": "major", "source_name": "S",
               "source_url": "https://example.com/x", "published_at": "2026-10-01T00:00:00Z"}
        self.assertIsNone(airdrops.normalize_seed_item(raw))
        raw["title"] = "普通空投"
        self.assertIsNotNone(airdrops.normalize_seed_item(raw))

    def test_feed_row_and_store_refuse_blocked(self):
        bad = {"title": "Guaranteed airdrop: send first to claim", "url": "https://blog.lido.fi/x", "guid": "g", "summary": "", "published_at": NOW}
        self.assertIsNone(ing.feed_item_to_row(bad, source_id="lido", source_name="Lido Blog", now_ms=NOW))
        # even if a row slipped through to the store it is dropped there
        tmp = tempfile.TemporaryDirectory()
        st = airdrops.AirdropStore(Path(tmp.name) / "a.sqlite")
        try:
            res = st.merge_ingested([_row(id="b1", title="先转账领取空投", source_url="https://example.com/b1")])
            self.assertEqual(res, {"added": 0, "merged": 0, "skipped": 0, "blocked": 1})
            self.assertEqual(st.list(origin="ingested")["total"], 0)
        finally:
            st.close()
            tmp.cleanup()

    def test_existing_blocked_rows_are_purged_on_open(self):
        tmp = tempfile.TemporaryDirectory()
        path = Path(tmp.name) / "a.sqlite"
        st = airdrops.AirdropStore(path)
        st._db.execute(
            "INSERT INTO items(id,title,project_name,category,source_name,source_url,summary,chains,status,published_at,updated_at,external_url,tags,seed_hash,origin) "
            "VALUES ('old','保证收益 空投','P','community','S','https://example.com/old','','','active',?,?,'','ingested','','ingested')", (NOW, NOW))
        st.close()
        st2 = airdrops.AirdropStore(path)
        try:
            self.assertIsNone(st2.get("old"))
        finally:
            st2.close()
            tmp.cleanup()


class SameActivityTests(unittest.TestCase):
    def a(self, title, pub=NOW, url="https://example.com/1", sid="s1", summary=""):
        return {"title": title, "summary": summary, "url": url, "published_at": pub, "source_id": sid}

    def test_identical_url_and_tracking_params(self):
        self.assertTrue(q.same_activity(self.a("A", url="https://x.com/p?utm_source=rss"), self.a("Totally different", url="https://x.com/p/", pub=NOW + 60 * DAY)))

    def test_high_title_overlap_merges(self):
        self.assertTrue(q.same_activity(
            self.a("Arbitrum launches community airdrop for early users", url="https://a.com/1"),
            self.a("Arbitrum launches community airdrop for early users (details)", url="https://b.com/2", sid="s2")))

    def test_shared_ticker_and_event_class_merges_across_publishers(self):
        a = self.a("OKX Jumpstart: KII (Kii Alpha) token airdrop for OKB holders", url="https://okx.com/1", sid="okx")
        b = self.a("Bybit KII Alpha Token Splash — share 2,000,000 KII airdrop pool", url="https://bybit.com/2", sid="bybit", pub=NOW + 2 * DAY)
        self.assertTrue(q.same_activity(a, b))

    def test_different_projects_same_event_do_not_merge(self):
        a = self.a("Arbitrum community airdrop claim opens", url="https://a.com/1", sid="s1")
        b = self.a("Optimism community airdrop claim opens", url="https://b.com/2", sid="s2")
        self.assertFalse(q.same_activity(a, b))

    def test_same_event_class_but_no_shared_entity_does_not_merge(self):
        a = self.a("New airdrop season announced by Alpha", sid="s1", url="https://a.com/1")
        b = self.a("Another airdrop wave from Beta Labs", sid="s2", url="https://b.com/2")
        self.assertFalse(q.same_activity(a, b))

    def test_outside_window_does_not_merge(self):
        a = self.a("Arbitrum launches community airdrop for early users", url="https://a.com/1")
        b = self.a("Arbitrum launches community airdrop for early users", url="https://b.com/2", sid="s2", pub=NOW + 30 * DAY)
        self.assertFalse(q.same_activity(a, b))

    def test_same_publisher_needs_title_overlap(self):
        a = self.a("Foo Token Splash — win FOO", sid="bybit", url="https://bybit.com/1")
        b = self.a("Foo airdrop quiz — win FOO", sid="bybit", url="https://bybit.com/2")
        self.assertFalse(q.same_activity(a, b))

    def test_verification_label(self):
        v = q.verification
        self.assertEqual(v([{"source_id": "okx", "kind": "official"}])["label"], "confirmed")
        self.assertEqual(v([{"source_id": "coindesk", "kind": "media"}])["label"], "unverified")
        self.assertEqual(v([{"source_id": "coindesk", "kind": "media"}, {"source_id": "coindesk", "kind": "media"}])["label"], "unverified")
        two = v([{"source_id": "coindesk", "kind": "media"}, {"source_id": "cointelegraph", "kind": "media"}])
        self.assertEqual((two["label"], two["sourceCount"], two["official"]), ("confirmed", 2, False))
        self.assertEqual(v([])["label"], "unverified")


class ParserTests(unittest.TestCase):
    def test_okx_real_fixture(self):
        raw = (FIX / "airdrops_okx_real_20261015.json").read_bytes()
        items = ing.parse_okx(raw, NOW)
        self.assertEqual(len(items), 6)
        for it in items:
            self.assertTrue(it["url"].startswith("https://www.okx.com/"))
            self.assertGreater(it["published_at"], 1_700_000_000_000)
        with self.assertRaises(ValueError):
            ing.parse_okx(b'{"code":"50011","msg":"rate","data":[]}', NOW)

    def test_bybit_real_fixture(self):
        raw = (FIX / "airdrops_bybit_real_20261015.json").read_bytes()
        items = ing.parse_bybit(raw, NOW)
        self.assertEqual(len(items), 5)
        self.assertTrue(all(i["url"].startswith("https://announcements.bybit.com/") for i in items))
        with self.assertRaises(ValueError):
            ing.parse_bybit(b'{"retCode":10001,"retMsg":"x"}', NOW)
        with self.assertRaises(json.JSONDecodeError):
            ing.parse_bybit(b"<html>blocked</html>", NOW)

    def test_exchange_filter_keeps_airdrop_style_only(self):
        meta = ing.FEED_SOURCES["bybit"]
        items = ing.parse_bybit((FIX / "airdrops_bybit_real_20261015.json").read_bytes(), NOW)
        rows = [ing.feed_item_to_row(i, source_id="bybit", source_name=meta["name"], now_ms=NOW, meta=meta) for i in items]
        rows = [r for r in rows if r]
        self.assertTrue(rows)
        self.assertTrue(all("Token Splash" in r["title"] or "irdrop" in r["title"] or "irdrop" in r["summary"] for r in rows), [r["title"] for r in rows])
        for r in rows:
            self.assertEqual(r["kind"], "official")
            self.assertLessEqual(len(r["summary"]), ing.SUMMARY_SHORT)
            self.assertIn("official", r["tags"].split(","))
            self.assertEqual(r["project_name"], "Bybit")

    def test_official_host_allowlist_and_tracking_strip(self):
        meta = ing.FEED_SOURCES["layerzero"]
        ok = {"title": "LayerZero airdrop snapshot explained", "url": "https://medium.com/layerzero-official/x-1?source=rss-abc", "guid": "g", "summary": "", "published_at": NOW}
        row = ing.feed_item_to_row(ok, source_id="layerzero", source_name=meta["name"], now_ms=NOW, meta=meta)
        self.assertEqual(row["source_url"], "https://medium.com/layerzero-official/x-1")
        evil = dict(ok, url="https://phish.example.net/claim-airdrop")
        self.assertIsNone(ing.feed_item_to_row(evil, source_id="layerzero", source_name=meta["name"], now_ms=NOW, meta=meta))
        http_only = dict(ok, url="http://medium.com/layerzero-official/x-1")
        self.assertIsNone(ing.feed_item_to_row(http_only, source_id="layerzero", source_name=meta["name"], now_ms=NOW, meta=meta))

    def test_old_items_dropped(self):
        meta = ing.FEED_SOURCES["lido"]
        old = {"title": "Lido airdrop 2023 recap", "url": "https://blog.lido.fi/x", "guid": "g", "summary": "", "published_at": NOW - 120 * DAY}
        self.assertIsNone(ing.feed_item_to_row(old, source_id="lido", source_name=meta["name"], now_ms=NOW, meta=meta))

    def test_binance_is_not_a_source(self):
        self.assertNotIn("binance", ing.FEED_SOURCES)
        for meta in ing.FEED_SOURCES.values():
            self.assertNotIn("binance.com", meta["url"])
            self.assertNotIn("/bapi/", meta["url"])


class MergeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.st = airdrops.AirdropStore(Path(self.tmp.name) / "a.sqlite")

    def tearDown(self):
        self.st.close()
        self.tmp.cleanup()

    def ing(self):
        return self.st.list(origin="ingested", size=50)

    def test_single_media_source_is_unverified(self):
        self.st.merge_ingested([_row(id="m1", title="Foo protocol airdrop opens", source_url="https://c.com/1", source_name="CoinDesk", source_id="coindesk")])
        it = self.ing()["items"][0]
        self.assertEqual((it["verification"], it["sourceCount"], it["official"]), ("unverified", 1, False))
        self.assertIn("先转账", it["risk"])

    def test_two_media_sources_merge_and_confirm(self):
        res = self.st.merge_ingested([
            _row(id="m1", title="Foo Labs announces FOO airdrop for early users", source_url="https://c.com/1", source_name="CoinDesk", source_id="coindesk"),
            _row(id="m2", title="Foo Labs announces FOO airdrop for early users - what to know", source_url="https://t.com/2", source_name="Cointelegraph", source_id="cointelegraph", published_at=NOW + 3600_000),
        ])
        self.assertEqual((res["added"], res["merged"]), (1, 1))
        d = self.ing()
        self.assertEqual(d["total"], 1)
        it = d["items"][0]
        self.assertEqual((it["verification"], it["sourceCount"]), ("confirmed", 2))
        self.assertEqual({s["name"] for s in it["sources"]}, {"CoinDesk", "Cointelegraph"})

    def test_official_post_promotes_card_and_confirms(self):
        self.st.merge_ingested([_row(id="m1", title="Arbitrum community airdrop claim opens today", source_url="https://c.com/1", source_name="CoinDesk", source_id="coindesk", project_name="行业资讯")])
        res = self.st.merge_ingested([_row(
            id="o1", title="Arbitrum community airdrop claim opens", source_url="https://blog.arbitrum.io/claim", source_name="Arbitrum Blog",
            source_id="arbitrum", kind="official", project_name="Arbitrum", published_at=NOW + 3600_000, tags="ingested,official")])
        self.assertEqual(res["merged"], 1)
        it = self.ing()["items"][0]
        self.assertEqual(self.ing()["total"], 1)
        self.assertEqual((it["verification"], it["official"], it["sourceCount"]), ("confirmed", True, 2))
        self.assertEqual(it["sourceName"], "Arbitrum Blog")
        self.assertEqual(it["projectName"], "Arbitrum")
        self.assertEqual(it["externalUrl"], "https://blog.arbitrum.io/claim")
        self.assertEqual(it["publishedAt"], NOW)  # earliest report time kept
        self.assertEqual(it["sources"][0]["kind"], "official")

    def test_official_alone_is_confirmed_even_with_one_source(self):
        self.st.merge_ingested([_row(id="o1", title="Bybit KII Token Splash", source_url="https://announcements.bybit.com/x", source_name="Bybit 公告", source_id="bybit", kind="official")])
        it = self.ing()["items"][0]
        self.assertEqual((it["verification"], it["sourceCount"]), ("confirmed", 1))

    def test_same_publisher_repeat_does_not_inflate_sources(self):
        self.st.merge_ingested([
            _row(id="b1", title="USDC Token Splash — Grab a share of the 100000 USDC prize pool", source_url="https://announcements.bybit.com/a", source_name="Bybit 公告", source_id="bybit", kind="official"),
            _row(id="b2", title="USDC Token Splash — Grab a share of the 100000 USDC prize pool", source_url="https://announcements.bybit.com/b", source_name="Bybit 公告", source_id="bybit", kind="official", published_at=NOW + DAY),
        ])
        d = self.ing()
        self.assertEqual(d["total"], 1)
        self.assertEqual(d["items"][0]["sourceCount"], 1)
        self.assertEqual(len(d["items"][0]["sources"]), 2)

    def test_different_campaigns_stay_separate(self):
        self.st.merge_ingested([
            _row(id="a", title="Arbitrum community airdrop claim opens", source_url="https://a.com/1", source_id="s1"),
            _row(id="b", title="Optimism community airdrop claim opens", source_url="https://b.com/2", source_id="s2"),
        ])
        self.assertEqual(self.ing()["total"], 2)

    def test_same_url_twice_is_skipped_not_double_counted(self):
        r = _row(id="a", title="Foo airdrop", source_url="https://a.com/1")
        self.assertEqual(self.st.merge_ingested([r])["added"], 1)
        again = self.st.merge_ingested([r])
        self.assertEqual((again["added"], again["merged"], again["skipped"]), (0, 0, 1))
        self.assertEqual(self.ing()["items"][0]["sourceCount"], 1)

    def test_curated_item_is_unverified_and_never_overwritten(self):
        cur = self.st.list(origin="curated", size=100)["items"]
        self.assertTrue(cur)
        self.assertTrue(all(i["verification"] == "unverified" and i["sourceCount"] == 1 for i in cur))
        c0 = cur[0]
        res = self.st.merge_ingested([_row(id="z", title="whatever", source_url=c0["sourceUrl"])])
        self.assertEqual(res["skipped"], 1)
        self.assertEqual(self.st.get(c0["id"])["title"], c0["title"])

    def test_verification_filter_and_counts(self):
        self.st.merge_ingested([_row(id="o1", title="Bybit KII Token Splash", source_url="https://announcements.bybit.com/x", source_name="Bybit 公告", source_id="bybit", kind="official")])
        conf = self.st.list(verification="confirmed", size=50)
        self.assertEqual(conf["total"], 1)
        unv = self.st.list(verification="unverified", size=50)
        self.assertEqual(unv["total"], len(airdrops.load_seed()))
        self.assertEqual(conf["verificationCounts"], {"confirmed": 1, "unverified": len(airdrops.load_seed())})

    def test_prune_removes_sightings(self):
        self.st.merge_ingested([_row(id="a", title="Foo airdrop", source_url="https://a.com/1", published_at=NOW - 100 * DAY)])
        self.assertGreaterEqual(self.st.prune_ingested(NOW), 1)
        n = self.st._db.execute("SELECT COUNT(*) FROM sightings WHERE item_id='a'").fetchone()[0]
        self.assertEqual(n, 0)

    def test_pre_v2_database_is_migrated(self):
        path = Path(self.tmp.name) / "legacy.sqlite"
        db = sqlite3.connect(path)
        db.executescript(
            "CREATE TABLE items (id TEXT PRIMARY KEY, title TEXT NOT NULL, project_name TEXT NOT NULL, category TEXT NOT NULL, "
            "source_name TEXT NOT NULL, source_url TEXT NOT NULL DEFAULT '', summary TEXT NOT NULL DEFAULT '', chains TEXT NOT NULL DEFAULT '', "
            "status TEXT NOT NULL DEFAULT 'active', published_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, external_url TEXT NOT NULL DEFAULT '', "
            "tags TEXT NOT NULL DEFAULT '', seed_hash TEXT NOT NULL DEFAULT '', origin TEXT NOT NULL DEFAULT 'curated');"
            "INSERT INTO items VALUES ('ing-coindesk-1','Old airdrop news','行业资讯','community','CoinDesk','https://c.com/old','','','active',1,1,'https://c.com/old','ingested,rss,coindesk','','ingested');"
        )
        db.commit()
        db.close()
        st = airdrops.AirdropStore(path)
        try:
            it = st.get("ing-coindesk-1")
            self.assertEqual((it["verification"], it["sourceCount"]), ("unverified", 1))
            self.assertEqual(it["sources"][0]["sourceId"], "coindesk")
        finally:
            st.close()


class FetcherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.st = airdrops.AirdropStore(Path(self.tmp.name) / "a.sqlite")
        self.clock = [NOW]
        self._env = patch.dict(os.environ, {"AUU_AIRDROPS_FEEDS": "okx,bybit,lido", "AUU_ROLE": "primary"})
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self.st.close()
        self.tmp.cleanup()

    def fetcher(self, fetch):
        return ing.AirdropFetcher(self.st, fetch=fetch, now_ms=lambda: self.clock[0])

    def test_one_source_failing_does_not_stop_others_and_is_logged(self):
        bybit = (FIX / "airdrops_bybit_real_20261015.json").read_bytes()

        def fetch(url):
            if "okx.com" in url:
                raise TimeoutError("slow")
            if "lido.fi" in url:
                return b"<html>not xml"
            return bybit

        with self.assertLogs("auu.airdrops.ingest", level="WARNING") as cm:
            out = self.fetcher(fetch).tick()
        self.assertFalse(out["okx"]["ok"])
        self.assertFalse(out["lido"]["ok"])
        self.assertTrue(out["bybit"]["ok"])
        self.assertGreaterEqual(out["bybit"]["added"], 1)
        self.assertTrue(any("okx failed" in m for m in cm.output))
        feeds = {f["id"]: f for f in self.st.list()["feeds"]}
        self.assertFalse(feeds["okx"]["ok"])
        self.assertEqual(feeds["okx"]["fails"], 1)
        self.assertEqual(feeds["okx"]["error"], "network")
        self.assertTrue(feeds["bybit"]["ok"])
        # site keeps serving everything we already had
        self.assertGreaterEqual(self.st.list()["total"], len(airdrops.load_seed()))

    def test_min_interval_and_backoff(self):
        calls = []

        def fetch(url):
            calls.append(url)
            raise TimeoutError("x")

        f = self.fetcher(fetch)
        f.tick()
        self.assertEqual(len(calls), 3)
        self.clock[0] += 60_000  # +1 min: everything is in back-off
        out = f.tick()
        self.assertEqual(len(calls), 3)
        self.assertEqual(out["okx"].get("skipped"), "interval")
        self.clock[0] += 2 * 3600_000  # past 2 x 30 min
        f.tick()
        self.assertEqual(len(calls), 6)
        # after success the normal 30-min cadence returns
        good = (FIX / "airdrops_okx_real_20261015.json").read_bytes()
        f2 = self.fetcher(lambda url: good)
        self.clock[0] += 4 * 3600_000
        self.assertTrue(f2.tick()["okx"]["ok"])
        self.clock[0] += 10 * 60_000
        self.assertEqual(f2.tick()["okx"].get("skipped"), "interval")
        self.clock[0] += 25 * 60_000
        self.assertNotEqual(f2.tick()["okx"].get("skipped"), "interval")

    def test_standby_never_fetches(self):
        called = []
        with patch.dict(os.environ, {"AUU_ROLE": "standby", "AUU_AIRDROPS_INGEST": "on"}):
            self.assertFalse(ing.ingest_enabled())
            out = self.fetcher(lambda url: called.append(url) or b"").tick(force=True)
        self.assertEqual(out, {"standby": True})
        self.assertEqual(called, [])
        with patch.dict(os.environ, {"AUU_ROLE": "primary", "AUU_AIRDROPS_INGEST": "on"}):
            self.assertTrue(ing.ingest_enabled())

    def test_okx_and_bybit_requests_are_single_page_keyless(self):
        urls = []
        self.fetcher(lambda u: urls.append(u) or b"{}").tick()
        for u in urls:
            self.assertNotIn("apikey", u.lower())
        self.assertEqual(sum(1 for u in urls if "okx.com" in u), 1)
        self.assertEqual(sum(1 for u in urls if "bybit.com" in u), 1)


class RouteV2Tests(unittest.TestCase):
    _ENV = ("AUU_AUTH", "AUU_ALLOW_SIGNUP", "AUU_USER_STORE", "AUU_USER_JOURNAL_DIR", "AUU_AUTH_RATE_MAX", "AUU_LEGACY_PUMP", "AUU_INVITE_CODE", "AUU_ADMIN_USER")

    def setUp(self):
        from app.auth.accounts import reset_accounts

        self.tmp = tempfile.TemporaryDirectory()
        self._prev = {k: os.environ.get(k) for k in self._ENV}
        root = Path(self.tmp.name)
        os.environ.update({"AUU_AUTH": "on", "AUU_ALLOW_SIGNUP": "on", "AUU_USER_STORE": str(root / "users.json"),
                           "AUU_USER_JOURNAL_DIR": str(root / "journals"), "AUU_AUTH_RATE_MAX": "1000", "AUU_LEGACY_PUMP": "off"})
        os.environ.pop("AUU_INVITE_CODE", None)
        os.environ.pop("AUU_ADMIN_USER", None)
        reset_accounts()
        self.store = airdrops.AirdropStore(root / "airdrops.sqlite")
        airdrops.reset_store(self.store)
        self._sock = patch.object(socket.socket, "connect", side_effect=AssertionError("network access in test"))
        self._sock.start()
        from fastapi.testclient import TestClient

        from app.main import create_app

        self.c = TestClient(create_app(legacy=False))
        pw = "paperPass123"
        self.assertEqual(self.c.post("/api/v1/auth/register", json={"name": "vin", "password": pw, "password_confirm": pw}).status_code, 200)

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

    def test_fields_filter_and_risk_notice(self):
        self.store.merge_ingested([_row(id="o1", title="Bybit KII Token Splash", source_url="https://announcements.bybit.com/x", source_name="Bybit 公告", source_id="bybit", kind="official", tags="ingested,official")])
        d = self.c.get("/api/v1/airdrops?size=50").json()["data"]
        self.assertIn("先转账", d["riskNotice"])
        self.assertIn("已确认", d["note"])
        for it in d["items"]:
            self.assertIn(it["verification"], {"confirmed", "unverified"})
            self.assertIn("先转账", it["risk"])
            self.assertGreaterEqual(it["sourceCount"], 1)
            self.assertTrue(it["sources"])
        conf = self.c.get("/api/v1/airdrops?verification=confirmed").json()["data"]
        self.assertEqual([i["id"] for i in conf["items"]], ["o1"])
        unv = self.c.get("/api/v1/airdrops?verification=unverified&size=50").json()["data"]
        self.assertTrue(all(i["verification"] == "unverified" for i in unv["items"]))
        self.assertEqual(self.c.get("/api/v1/airdrops?verification=bogus").status_code, 400)
        det = self.c.get("/api/v1/airdrops/o1").json()["data"]
        self.assertEqual(det["item"]["verification"], "confirmed")
        self.assertIn("先转账", det["riskNotice"])
        self.assertTrue(any(f["tier"] == "official" for f in d["feeds"]))


if __name__ == "__main__":
    unittest.main()
