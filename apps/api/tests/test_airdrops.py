"""空投雷达: seed load, filters, detail, login-only route."""
from __future__ import annotations

import os
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import airdrops

SEED = airdrops.load_seed()


class SeedTests(unittest.TestCase):
    def test_bundled_seed_is_valid_and_attributed(self):
        self.assertGreaterEqual(len(SEED), 8)
        ids = {i["id"] for i in SEED}
        self.assertEqual(len(ids), len(SEED))
        self.assertIn("meme", airdrops.CATEGORIES)
        meme_n = sum(1 for i in SEED if i["category"] == "meme")
        self.assertGreaterEqual(meme_n, 6)
        for i in SEED:
            self.assertIn(i["category"], airdrops.CATEGORIES)
            self.assertIn(i["status"], airdrops.STATUSES)
            self.assertTrue(i["source_name"])
            self.assertTrue(i["source_url"].startswith("https://"))
            self.assertTrue((i["external_url"] or i["source_url"]).startswith("https://"))
            self.assertGreater(i["published_at"], 0)

    def test_normalize_rejects_bad_rows(self):
        self.assertIsNone(airdrops.normalize_seed_item({"id": "x", "title": "t"}))
        self.assertIsNone(
            airdrops.normalize_seed_item(
                {
                    "id": "x",
                    "title": "t",
                    "project_name": "p",
                    "category": "major",
                    "source_name": "S",
                    "status": "bogus",
                    "published_at": "2026-01-01T00:00:00Z",
                }
            )
        )


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.store = airdrops.AirdropStore(root / "airdrops.sqlite")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_list_filters_and_detail(self):
        all_page = self.store.list(category="all", size=50)
        self.assertEqual(all_page["total"], len(SEED))
        self.assertEqual(len(all_page["items"]), len(SEED))
        major = self.store.list(category="major", size=50)
        community = self.store.list(category="community", size=50)
        meme = self.store.list(category="meme", size=50)
        self.assertEqual(major["total"] + community["total"] + meme["total"], all_page["total"])
        self.assertTrue(all(i["category"] == "major" for i in major["items"]))
        self.assertGreaterEqual(meme["total"], 6)
        self.assertTrue(all(i["category"] == "meme" for i in meme["items"]))
        self.assertTrue(all(i.get("sourceName") and (i.get("sourceUrl") or "").startswith("https://") for i in meme["items"]))
        ended = self.store.list(status="ended", size=50)
        self.assertTrue(ended["total"] >= 1)
        self.assertTrue(all(i["status"] == "ended" for i in ended["items"]))
        first = all_page["items"][0]
        detail = self.store.get(first["id"])
        self.assertIsNotNone(detail)
        self.assertEqual(detail["id"], first["id"])
        self.assertIn("sourceName", detail)
        self.assertIsNone(self.store.get("does-not-exist"))

    def test_seed_idempotent(self):
        n1 = self.store.ensure_seeded()
        n2 = self.store.ensure_seeded()
        self.assertEqual(n1, 0)  # already seeded in __init__
        self.assertEqual(n2, 0)
        n3 = self.store.ensure_seeded(force=True)
        self.assertEqual(n3, len(SEED))


class RouteTests(unittest.TestCase):
    _ENV = (
        "AUU_AUTH",
        "AUU_ALLOW_SIGNUP",
        "AUU_USER_STORE",
        "AUU_USER_JOURNAL_DIR",
        "AUU_AUTH_RATE_MAX",
        "AUU_LEGACY_PUMP",
        "AUU_INVITE_CODE",
        "AUU_ADMIN_USER",
    )

    def setUp(self):
        from app.auth.accounts import reset_accounts

        self.tmp = tempfile.TemporaryDirectory()
        self._prev = {k: os.environ.get(k) for k in self._ENV}
        root = Path(self.tmp.name)
        os.environ.update(
            {
                "AUU_AUTH": "on",
                "AUU_ALLOW_SIGNUP": "on",
                "AUU_USER_STORE": str(root / "users.json"),
                "AUU_USER_JOURNAL_DIR": str(root / "journals"),
                "AUU_AUTH_RATE_MAX": "1000",
                "AUU_LEGACY_PUMP": "off",
            }
        )
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
        self.assertEqual(
            self.c.post("/api/v1/auth/register", json={"name": "vin", "password": pw, "password_confirm": pw}).status_code,
            200,
        )

    def test_login_required_list_and_detail(self):
        self.assertEqual(self.c.get("/api/v1/airdrops").status_code, 401)
        self.assertEqual(self.c.get("/api/v1/airdrops/monad-mon-airdrop-results").status_code, 401)
        self._login()
        r = self.c.get("/api/v1/airdrops?category=major&size=5")
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()["data"]
        self.assertGreaterEqual(d["total"], 1)
        self.assertLessEqual(len(d["items"]), 5)
        self.assertTrue(all(i["category"] == "major" for i in d["items"]))
        it = d["items"][0]
        for key in (
            "id",
            "title",
            "projectName",
            "category",
            "sourceName",
            "sourceUrl",
            "summary",
            "chains",
            "status",
            "publishedAt",
            "updatedAt",
            "externalUrl",
            "tags",
            "origin",
        ):
            self.assertIn(key, it)
        self.assertTrue(it["sourceName"])
        self.assertIn("仅供参考", d["note"])
        detail = self.c.get(f"/api/v1/airdrops/{it['id']}")
        self.assertEqual(detail.status_code, 200)
        body = detail.json()["data"]["item"]
        self.assertEqual(body["id"], it["id"])
        meme = self.c.get("/api/v1/airdrops?category=meme&size=50")
        self.assertEqual(meme.status_code, 200, meme.text)
        md = meme.json()["data"]
        self.assertGreaterEqual(md["total"], 6)
        self.assertTrue(all(i["category"] == "meme" for i in md["items"]))
        self.assertIn("meme", md["categories"])
        self.assertEqual(self.c.get("/api/v1/airdrops?category=evil").status_code, 400)
        self.assertEqual(self.c.get("/api/v1/airdrops?status=nope").status_code, 400)
        self.assertEqual(self.c.get("/api/v1/airdrops/does-not-exist").status_code, 404)

    def test_store_error_is_503(self):
        self._login()
        with patch.object(self.store, "list", side_effect=RuntimeError("boom")):
            r = self.c.get("/api/v1/airdrops")
        self.assertEqual(r.status_code, 503)
        self.assertEqual(self.c.get("/api/v1/status").status_code, 200)


if __name__ == "__main__":
    unittest.main()
