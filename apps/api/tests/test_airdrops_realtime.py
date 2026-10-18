"""空投雷达 realtime step: 240 s tick, per-source politeness floors, conditional requests (304),
Retry-After, 301/308 redirect following (Optimism/Paragraph), back-off kept, standby never fetches."""
from __future__ import annotations

import gzip
import json
import os
import tempfile
import threading
import unittest
from email.utils import formatdate
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from app import airdrops
from app.airdrops import ingest as ing

FIX = Path(__file__).resolve().parent / "fixtures"
NOW = 1792062900000
SEC = 1000

RSS_AIR = b"""<?xml version="1.0"?>
<rss version="2.0"><channel><title>Blog</title>
<item><title>Optimism: Season 9 airdrop details announced</title>
<link>https://paragraph.com/@the-optimism-collective/season-9-airdrop</link><guid>g-s9</guid>
<pubDate>Sun, 18 Oct 2026 10:00:00 +0000</pubDate><description>Eligibility and claim portal.</description></item>
<item><title>Optimism: guaranteed returns airdrop, send first</title>
<link>https://paragraph.com/@the-optimism-collective/scam</link><guid>g-scam</guid>
<pubDate>Sun, 18 Oct 2026 10:05:00 +0000</pubDate><description>guaranteed returns</description></item>
</channel></rss>"""


class IntervalConfigTests(unittest.TestCase):
    def setUp(self):
        self._env = patch.dict(os.environ, {}, clear=False)
        self._env.start()
        for k in ("AUU_AIRDROPS_INTERVAL_SEC", "AUU_AIRDROPS_MIN_API_SEC", "AUU_AIRDROPS_MIN_RSS_SEC", "AUU_AIRDROPS_MIN_MEDIA_SEC"):
            os.environ.pop(k, None)

    def tearDown(self):
        self._env.stop()

    def test_default_tick_is_240_and_env_tunable(self):
        self.assertEqual(ing.interval_sec(), 240)
        for raw, want in (("300", 300), ("5", 60), ("999999", 3600), ("junk", 240), ("", 240)):
            with patch.dict(os.environ, {"AUU_AIRDROPS_INTERVAL_SEC": raw}):
                self.assertEqual(ing.interval_sec(), want, raw)

    def test_per_class_floors(self):
        f = ing.FEED_SOURCES
        self.assertEqual(ing.min_interval_for(f["okx"]), 180)
        self.assertEqual(ing.min_interval_for(f["bybit"]), 180)
        self.assertEqual(ing.min_interval_for(f["lido"]), 240)
        self.assertEqual(ing.min_interval_for(f["coindesk"]), 240)
        self.assertEqual(ing.min_interval_for(f["cointelegraph"]), 240)
        # Medium declares Cache-Control max-age=900 -> x4 = 960 s; Paragraph (ETag, 5 min CDN) is a normal blog
        self.assertEqual(ing.min_interval_for(f["layerzero"]), 960)
        self.assertEqual(ing.min_interval_for(f["optimism"]), 240)

    def test_floor_env_and_hard_minimum(self):
        f = ing.FEED_SOURCES
        with patch.dict(os.environ, {"AUU_AIRDROPS_MIN_API_SEC": "600", "AUU_AIRDROPS_MIN_RSS_SEC": "900", "AUU_AIRDROPS_MIN_MEDIA_SEC": "300"}):
            self.assertEqual(ing.min_interval_for(f["okx"]), 600)
            self.assertEqual(ing.min_interval_for(f["sui"]), 900)
            self.assertEqual(ing.min_interval_for(f["coindesk"]), 300)
            self.assertEqual(ing.min_interval_for(f["layerzero"]), 3600)
        with patch.dict(os.environ, {"AUU_AIRDROPS_MIN_API_SEC": "1", "AUU_AIRDROPS_MIN_RSS_SEC": "0", "AUU_AIRDROPS_MIN_MEDIA_SEC": "-5"}):
            for sid, meta in f.items():
                self.assertGreaterEqual(ing.min_interval_for(meta), ing.HARD_MIN_INTERVAL, sid)
        with patch.dict(os.environ, {"AUU_AIRDROPS_MIN_API_SEC": "abc"}):
            self.assertEqual(ing.min_interval_for(f["okx"]), 180)

    def test_optimism_points_at_the_non_redirecting_feed(self):
        m = ing.FEED_SOURCES["optimism"]
        self.assertTrue(m["url"].startswith("https://paragraph.com/api/blogs/rss/"))
        self.assertTrue(ing.host_allowed("https://paragraph.com/@the-optimism-collective/x", m["hosts"]))
        self.assertTrue(ing.host_allowed("https://optimism.mirror.xyz/old-post", m["hosts"]))


class _Origin:
    """Tiny local HTTP origin with scripted routes; records request headers."""

    def __init__(self):
        self.hits: list[tuple[str, dict]] = []
        self.routes: dict[str, callable] = {}
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                hdrs = {k.lower(): v for k, v in self.headers.items()}
                outer.hits.append((self.path, hdrs))
                fn = outer.routes.get(self.path.split("?")[0])
                code, headers, body = fn(hdrs) if fn else (404, {}, b"")
                self.send_response(code)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


class HttpFetchCondTests(unittest.TestCase):
    def setUp(self):
        self.o = _Origin()

    def tearDown(self):
        self.o.close()

    def test_sends_ua_accept_encoding_and_no_cookie(self):
        self.o.routes["/f"] = lambda h: (200, {"ETag": '"v1"', "Last-Modified": "Sun, 18 Oct 2026 10:00:00 GMT"}, b"<rss/>")
        r = ing.http_fetch_cond(self.o.base + "/f", accept="application/rss+xml")
        self.assertEqual((r.status, r.body, r.etag), (200, b"<rss/>", '"v1"'))
        self.assertEqual(r.last_modified, "Sun, 18 Oct 2026 10:00:00 GMT")
        h = self.o.hits[0][1]
        self.assertTrue(h["user-agent"].startswith("AUUTRADE-airdrops/"))
        self.assertIn("gzip", h["accept-encoding"])
        self.assertEqual(h["accept"], "application/rss+xml")
        self.assertNotIn("cookie", h)
        self.assertNotIn("authorization", h)
        self.assertNotIn("if-none-match", h)

    def test_conditional_headers_and_304(self):
        def fn(h):
            if h.get("if-none-match") == '"v1"':
                return 304, {"ETag": '"v1"'}, b""
            return 200, {"ETag": '"v1"'}, b"data"

        self.o.routes["/f"] = fn
        r = ing.http_fetch_cond(self.o.base + "/f", etag='"v1"', last_modified="Sun, 18 Oct 2026 10:00:00 GMT")
        self.assertTrue(r.not_modified)
        self.assertEqual(r.body, b"")
        self.assertEqual(r.etag, '"v1"')
        h = self.o.hits[0][1]
        self.assertEqual(h["if-none-match"], '"v1"')
        self.assertEqual(h["if-modified-since"], "Sun, 18 Oct 2026 10:00:00 GMT")
        self.assertFalse(ing.http_fetch_cond(self.o.base + "/f", etag='"old"').not_modified)

    def test_follows_301_then_308_to_the_feed(self):
        """The Optimism case: mirror.xyz 301 -> paragraph.com 308 -> api host 200."""
        self.o.routes["/a"] = lambda h: (301, {"Location": "/b"}, b"")
        self.o.routes["/b"] = lambda h: (308, {"Location": self.o.base + "/c?x=1"}, b"")
        self.o.routes["/c"] = lambda h: (200, {}, b"final")
        r = ing.http_fetch_cond(self.o.base + "/a")
        self.assertEqual((r.status, r.body), (200, b"final"))
        self.assertTrue(r.final_url.endswith("/c?x=1"))
        self.assertEqual([p for p, _ in self.o.hits], ["/a", "/b", "/c?x=1"])
        # each hop still has our UA
        self.assertTrue(all(h["user-agent"].startswith("AUUTRADE-airdrops/") for _, h in self.o.hits))

    def test_308_alone_is_followed(self):
        self.o.routes["/a"] = lambda h: (308, {"Location": "/b"}, b"")
        self.o.routes["/b"] = lambda h: (200, {}, b"ok")
        self.assertEqual(ing.http_fetch_cond(self.o.base + "/a").body, b"ok")

    def test_redirect_loop_and_bad_redirects_fail_cleanly(self):
        self.o.routes["/loop"] = lambda h: (308, {"Location": "/loop"}, b"")
        with self.assertRaisesRegex(ValueError, "too many redirects"):
            ing.http_fetch_cond(self.o.base + "/loop", max_redirects=3)
        self.assertEqual(len(self.o.hits), 4)
        self.o.routes["/nol"] = lambda h: (308, {}, b"")
        with self.assertRaisesRegex(ValueError, "without Location"):
            ing.http_fetch_cond(self.o.base + "/nol")
        self.o.routes["/ftp"] = lambda h: (302, {"Location": "ftp://example.com/x"}, b"")
        with self.assertRaisesRegex(ValueError, "unsupported"):
            ing.http_fetch_cond(self.o.base + "/ftp")

    def test_https_to_http_downgrade_is_refused(self):
        class FakeHeaders(dict):
            def get(self, k, d=None):
                return super().get(k, d)

        calls = []

        class FakeOpener:
            def open(self, req, timeout=0):
                calls.append(req.full_url)
                raise HTTPError(req.full_url, 308, "Permanent Redirect", FakeHeaders({"Location": "http://evil.example/x"}), None)

        with patch.object(ing, "_no_redirect_opener", lambda: FakeOpener()):
            with self.assertRaisesRegex(ValueError, "downgrades"):
                ing.http_fetch_cond("https://paragraph.com/api/blogs/rss/@x")
        self.assertEqual(calls, ["https://paragraph.com/api/blogs/rss/@x"])

    def test_errors_propagate_for_backoff_and_gzip_is_decoded(self):
        self.o.routes["/e"] = lambda h: (503, {"Retry-After": "120"}, b"")
        with self.assertRaises(HTTPError) as cm:
            ing.http_fetch_cond(self.o.base + "/e")
        self.assertEqual(cm.exception.code, 503)
        self.o.routes["/z"] = lambda h: (200, {"Content-Encoding": "gzip"}, gzip.compress(b"<rss>hello</rss>"))
        self.assertEqual(ing.http_fetch_cond(self.o.base + "/z").body, b"<rss>hello</rss>")

    def test_size_caps_apply_to_raw_and_decompressed_bodies(self):
        self.o.routes["/big"] = lambda h: (200, {}, b"x" * 5000)
        with self.assertRaisesRegex(ValueError, "larger"):
            ing.http_fetch_cond(self.o.base + "/big", max_bytes=1000)
        self.o.routes["/bomb"] = lambda h: (200, {"Content-Encoding": "gzip"}, gzip.compress(b"a" * 200_000))
        with self.assertRaisesRegex(ValueError, "larger"):
            ing.http_fetch_cond(self.o.base + "/bomb", max_bytes=10_000)

    def test_retry_after_parsing(self):
        mk = lambda code, v: HTTPError("u", code, "x", {"Retry-After": v} if v is not None else {}, None)
        self.assertEqual(ing._retry_after_sec(mk(429, "90")), 90)
        self.assertEqual(ing._retry_after_sec(mk(503, "999999")), ing.MAX_BACKOFF_SEC)
        self.assertEqual(ing._retry_after_sec(mk(429, "nonsense")), 0)
        self.assertEqual(ing._retry_after_sec(mk(500, "90")), 0)
        self.assertEqual(ing._retry_after_sec(mk(429, None)), 0)
        self.assertEqual(ing._retry_after_sec(TimeoutError()), 0)
        future = formatdate(__import__("time").time() + 300, usegmt=True)
        self.assertTrue(200 <= ing._retry_after_sec(mk(429, future)) <= 300)


class FetcherRealtimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.st = airdrops.AirdropStore(Path(self.tmp.name) / "a.sqlite")
        self.clock = [NOW]
        self._env = patch.dict(os.environ, {"AUU_ROLE": "primary", "AUU_AIRDROPS_FEEDS": "okx,bybit,lido,layerzero,optimism"})
        self._env.start()
        for k in ("AUU_AIRDROPS_MIN_API_SEC", "AUU_AIRDROPS_MIN_RSS_SEC", "AUU_AIRDROPS_MIN_MEDIA_SEC"):
            os.environ.pop(k, None)

    def tearDown(self):
        self._env.stop()
        self.st.close()
        self.tmp.cleanup()

    def fetcher(self, **kw):
        return ing.AirdropFetcher(self.st, now_ms=lambda: self.clock[0], **kw)

    def test_240s_ticks_hit_each_source_at_its_own_cadence(self):
        calls: dict[str, int] = {}

        def transport(meta, v):
            sid = next(k for k, m in ing.FEED_SOURCES.items() if m is meta)
            calls[sid] = calls.get(sid, 0) + 1
            return ing.FetchResult(304, etag='"e"')

        f = self.fetcher(transport=transport)
        for _ in range(12):  # 12 ticks x 240 s = 48 min
            f.tick()
            self.clock[0] += 240 * SEC + 3 * SEC  # + a few seconds of tick duration
        self.assertEqual(calls["okx"], 12)
        self.assertEqual(calls["bybit"], 12)
        self.assertEqual(calls["lido"], 12)
        self.assertEqual(calls["optimism"], 12)
        self.assertEqual(calls["layerzero"], 3)  # 960 s -> every 4th tick
        # never more often than the floor
        self.assertLessEqual(calls["okx"] * 180, 12 * 243)

    def test_conditional_validators_roundtrip_and_304_is_cheap(self):
        seen: list[dict] = []
        state = {"mode": 200}

        def transport(meta, v):
            if meta["kind"] != "rss" or meta["url"] != ing.FEED_SOURCES["lido"]["url"]:
                return ing.FetchResult(304)
            seen.append(dict(v))
            if state["mode"] == 304:
                return ing.FetchResult(304, etag=v.get("etag"), last_modified=v.get("last_modified"))
            return ing.FetchResult(200, RSS_AIR.replace(b"paragraph.com/@the-optimism-collective", b"blog.lido.fi"), '"abc"', "Sun, 18 Oct 2026 10:00:00 GMT")

        with patch.dict(os.environ, {"AUU_AIRDROPS_FEEDS": "lido"}):
            f = self.fetcher(transport=transport)
            out = f.tick()
            self.assertEqual(seen[0], {})  # first request is unconditional
            self.assertTrue(out["lido"]["ok"])
            self.assertEqual(out["lido"]["added"], 1)  # the scam line is dropped, the real one kept
            self.assertEqual(self.st.get_validators("lido"), {"etag": '"abc"', "last_modified": "Sun, 18 Oct 2026 10:00:00 GMT"})
            n_items = self.st.list()["total"]
            st1 = self.st.fetch_state("lido")

            state["mode"] = 304
            self.clock[0] += 250 * SEC
            out = f.tick()
            self.assertEqual(seen[1], {"etag": '"abc"', "last_modified": "Sun, 18 Oct 2026 10:00:00 GMT"})
            self.assertTrue(out["lido"]["ok"] and out["lido"]["notModified"])
            self.assertEqual(out["lido"]["added"], 0)
            self.assertEqual(self.st.list()["total"], n_items)
            st2 = self.st.fetch_state("lido")
            self.assertEqual(st2["last_ok"], self.clock[0])  # freshness timestamp advanced
            self.assertGreater(st2["last_ok"], st1["last_ok"])
            self.assertEqual(st2["last_items"], st1["last_items"])  # kept (nothing parsed)
            self.assertEqual(st2["fails"], 0)
            feeds = {x["id"]: x for x in self.st.list()["feeds"]}
            self.assertTrue(feeds["lido"]["ok"])
            self.assertEqual(self.st.list()["lastIngestAt"], self.clock[0])

            # origin changes -> 200 with a new validator replaces the stored one
            state["mode"] = 200
            self.clock[0] += 250 * SEC
            f.tick()
            self.assertEqual(self.st.get_validators("lido")["etag"], '"abc"')

    def test_validators_not_stored_when_parse_fails_and_cleared_when_absent(self):
        def bad(meta, v):
            return ing.FetchResult(200, b"<html>not a feed", '"zzz"')

        with patch.dict(os.environ, {"AUU_AIRDROPS_FEEDS": "lido"}):
            out = self.fetcher(transport=bad).tick()
            self.assertFalse(out["lido"]["ok"])
            self.assertEqual(self.st.get_validators("lido"), {})
            self.st.set_validators("lido", '"x"', None)
            self.assertEqual(self.st.get_validators("lido"), {"etag": '"x"', "last_modified": None})
            self.st.set_validators("lido", None, None)
            self.assertEqual(self.st.get_validators("lido"), {})

    def test_legacy_url_to_bytes_fetch_still_works(self):
        good = (FIX / "airdrops_okx_real_20261015.json").read_bytes()
        with patch.dict(os.environ, {"AUU_AIRDROPS_FEEDS": "okx"}):
            out = self.fetcher(fetch=lambda u: good).tick()
        self.assertTrue(out["okx"]["ok"])

    def test_failure_backoff_is_kept_and_retry_after_is_honoured(self):
        n = [0]

        def transport(meta, v):
            n[0] += 1
            raise HTTPError(meta["url"], 429, "slow down", {"Retry-After": "1200"}, None)

        with patch.dict(os.environ, {"AUU_AIRDROPS_FEEDS": "okx"}):
            f = self.fetcher(transport=transport)
            self.assertFalse(f.tick()["okx"]["ok"])
            self.assertEqual(n[0], 1)
            # Retry-After 20 min beats the 300 s back-off
            self.clock[0] += 900 * SEC
            self.assertEqual(f.tick()["okx"].get("skipped"), "interval")
            self.assertEqual(n[0], 1)
            self.clock[0] += 301 * SEC  # 1201 s after the 429
            f.tick()
            self.assertEqual(n[0], 2)
            # exponential back-off after consecutive failures is unchanged: fails=2 -> 180 x 4 = 720 s
            st = self.st.fetch_state("okx")
            self.assertEqual(st["fails"], 2)

    def test_plain_failures_double_up_to_cap_and_recover(self):
        n = [0]
        ok = [False]
        good = (FIX / "airdrops_okx_real_20261015.json").read_bytes()

        def transport(meta, v):
            n[0] += 1
            if ok[0]:
                return ing.FetchResult(200, good)
            raise TimeoutError("x")

        with patch.dict(os.environ, {"AUU_AIRDROPS_FEEDS": "okx"}):
            f = self.fetcher(transport=transport)
            f.tick()  # fail 1 -> wait max(360, 300)=360
            self.clock[0] += 300 * SEC
            f.tick()
            self.assertEqual(n[0], 1)
            self.clock[0] += 61 * SEC
            f.tick()  # fail 2 -> wait 720
            self.assertEqual(n[0], 2)
            self.clock[0] += 700 * SEC
            f.tick()
            self.assertEqual(n[0], 2)
            ok[0] = True
            self.clock[0] += 21 * SEC
            self.assertTrue(f.tick()["okx"]["ok"])
            self.assertEqual(self.st.fetch_state("okx")["fails"], 0)
            self.clock[0] += 100 * SEC
            self.assertEqual(f.tick()["okx"].get("skipped"), "interval")
            self.clock[0] += 81 * SEC
            self.assertTrue(f.tick()["okx"]["ok"])  # back to the 180 s floor

    def test_optimism_308_end_to_end_with_real_http(self):
        o = _Origin()
        try:
            o.routes["/feed/atom"] = lambda h: (301, {"Location": o.base + "/api/blogs/rss/@opt"}, b"")
            o.routes["/api/blogs/rss/@opt"] = lambda h: (308, {"Location": o.base + "/blogs/rss/@opt"}, b"")

            def feed(h):
                if h.get("if-none-match") == '"p1"':
                    return 304, {"ETag": '"p1"'}, b""
                return 200, {"ETag": '"p1"', "Content-Type": "text/xml"}, RSS_AIR

            o.routes["/blogs/rss/@opt"] = feed
            meta = dict(ing.FEED_SOURCES["optimism"], url=o.base + "/feed/atom")  # the OLD url, redirect chain included
            with patch.dict(ing.FEED_SOURCES, {"optimism": meta}), patch.dict(os.environ, {"AUU_AIRDROPS_FEEDS": "optimism"}):
                f = self.fetcher()
                out = f.tick()
                self.assertTrue(out["optimism"]["ok"], out)
                self.assertEqual(out["optimism"]["added"], 1)
                it = [i for i in self.st.list(origin="ingested")["items"] if "Season 9" in i["title"]][0]
                self.assertEqual(it["verification"], "confirmed")  # official source rule intact
                self.assertTrue(it["sourceUrl"].startswith("https://paragraph.com/@the-optimism-collective/"))
                self.assertFalse(any("guaranteed" in i["title"].lower() for i in self.st.list(size=50)["items"]))  # blacklist intact
                self.clock[0] += 500 * SEC
                out = f.tick()
                self.assertTrue(out["optimism"].get("notModified"))
                self.assertEqual([p for p, _ in o.hits].count("/blogs/rss/@opt"), 2)
        finally:
            o.close()

    def test_standby_never_fetches(self):
        n = [0]

        def transport(meta, v):
            n[0] += 1
            return ing.FetchResult(304)

        with patch.dict(os.environ, {"AUU_ROLE": "standby", "AUU_AIRDROPS_INGEST": "on"}):
            self.assertFalse(ing.ingest_enabled())
            self.assertEqual(self.fetcher(transport=transport).tick(force=True), {"standby": True})
        self.assertEqual(n[0], 0)


class FeedStatusTests(unittest.TestCase):
    def test_feed_status_has_floor(self):
        with tempfile.TemporaryDirectory() as tmp:
            st = airdrops.AirdropStore(Path(tmp) / "a.sqlite")
            try:
                feeds = {f["id"]: f for f in st.list()["feeds"]}
                self.assertEqual(feeds["okx"]["minIntervalSec"], 180)
                self.assertEqual(feeds["layerzero"]["minIntervalSec"], 960)
            finally:
                st.close()


if __name__ == "__main__":
    unittest.main()
