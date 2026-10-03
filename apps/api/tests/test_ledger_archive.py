"""Pump paper ledger archive: verified tar.gz, originals removed, restorable."""
from __future__ import annotations

import json
import tarfile
import tempfile
import unittest
from pathlib import Path

from app.paper import ledger_archive as la


class LedgerArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "evidence").mkdir()
        self.files = {
            "paper_journal.json": '{"closed": [1, 2], "lots": {"X/SOL": []}}',
            "paper_journal-round5-clean.json": "{}",
            "shadow_compare.json": '{"sets": []}',
            "trader_watchlist.json": "[]",
            "evidence/paper_evidence.jsonl": '{"a": 1}\n',
            "evidence/launch_tapes.jsonl.1": '{"t": 1}\n',
        }
        for rel, body in self.files.items():
            (self.root / rel).write_text(body)
        self.keep = {"users.json": '{"users": []}', "mainstream.sqlite": "db"}
        for rel, body in self.keep.items():
            (self.root / rel).write_text(body)

    def tearDown(self):
        self.tmp.cleanup()

    def test_dry_run_changes_nothing(self):
        res = la.archive(root=self.root)
        self.assertEqual(sorted(f["path"] for f in res["files"]), sorted(self.files))
        self.assertTrue(all((self.root / r).exists() for r in self.files))
        self.assertFalse((self.root / "archive").exists())

    def test_archive_then_restore(self):
        res = la.archive(apply=True, root=self.root, now=0)
        dest = Path(res["archive"])
        self.assertEqual(dest.name, "pump-ledger-19700101T000000Z.tar.gz")
        self.assertTrue(dest.exists())
        for rel in self.files:
            self.assertFalse((self.root / rel).exists(), rel)
        for rel in self.keep:
            self.assertTrue((self.root / rel).exists(), rel)
        self.assertEqual(la.collect(self.root), [])
        manifest = la.verify(dest)
        self.assertEqual(manifest["kind"], "auu-pump-paper-ledger")
        self.assertEqual(len(manifest["files"]), len(self.files))
        # Restore refuses to clobber a fresh ledger without --force.
        (self.root / "paper_journal.json").write_text("{}")
        blocked = la.restore(dest, apply=True, root=self.root)
        self.assertIn("error", blocked)
        self.assertEqual((self.root / "paper_journal.json").read_text(), "{}")
        done = la.restore(dest, apply=True, force=True, root=self.root)
        self.assertTrue(done["restored"])
        for rel, body in self.files.items():
            self.assertEqual((self.root / rel).read_text(), body, rel)

    def test_corrupt_archive_detected(self):
        dest = Path(la.archive(apply=True, root=self.root, now=0)["archive"])
        bad = self.root / "bad.tar.gz"
        with tarfile.open(dest, "r:gz") as src, tarfile.open(bad, "w:gz") as out:
            for m in src.getmembers():
                data = src.extractfile(m).read()
                if m.name == "paper_journal.json":
                    data = b"tampered"
                    m.size = len(data)
                import io

                out.addfile(m, io.BytesIO(data))
        with self.assertRaises(ValueError):
            la.verify(bad)

    def test_fresh_ledger_after_archive(self):
        import os
        from unittest.mock import patch

        la.archive(apply=True, root=self.root)
        with patch.dict(os.environ, {"AUU_DATA_DIR": str(self.root)}):
            os.environ.pop("PAPER_JOURNAL_STORE", None)
            from app.paper import ledger

            ledger._ledger = None
            try:
                j = ledger.get_paper_journal()
                self.assertEqual(len(j.closed), 0)
                self.assertEqual(sum(len(v) for v in j.lots.values()), 0)
            finally:
                ledger._ledger = None


class MainstreamConsoleStatsTests(unittest.TestCase):
    def test_fresh_mainstream_ledger_shows_pending_not_nogo(self):
        import os
        from unittest.mock import patch

        from app.paper import ledger
        from app.paper.events import console_stats

        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"AUU_LEGACY_PUMP": "off", "AUU_DATA_DIR": tmp}):
            os.environ.pop("PAPER_JOURNAL_STORE", None)
            ledger._ledger = None
            try:
                with patch("app.legacy.pump.paper.executability.build_executability", side_effect=AssertionError("pump verdict")):
                    stats = console_stats()
            finally:
                ledger._ledger = None
        self.assertEqual(stats["open_positions"], 0)
        self.assertEqual(stats["closed_today"], 0)
        self.assertEqual(stats["verdict"], "pending")
        self.assertEqual(stats["go_window_label"], "mainstream")
        self.assertFalse(stats["liveEnabled"])


if __name__ == "__main__":
    unittest.main()
