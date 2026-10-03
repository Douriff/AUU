"""ops/backup/auu_backup.py: remote snapshot script, manifest verification, restore drill, retention (no network)."""
from __future__ import annotations

import gzip
import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
API = ROOT / "apps" / "api"


def _load():
    spec = importlib.util.spec_from_file_location("auu_backup", ROOT / "ops" / "backup" / "auu_backup.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class OffsiteBackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.data = self.root / "data"
        self.data.mkdir()
        from app.paper.strategy_runner import StrategyLedger

        led = StrategyLedger(self.data / "mainstream_strategy.sqlite")
        led.close()
        for n in ("shadow_s3.sqlite", "mainstream_paper.sqlite"):
            con = sqlite3.connect(self.data / n)
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
            con.executemany("INSERT INTO t(v) VALUES (?)", [(str(i),) for i in range(50)])
            con.commit()  # rows stay in the WAL while this connection is open (like the running API)
            self.addCleanup(con.close)
        self.dest = self.root / "backups"
        with patch.dict(os.environ, {"AUU_BACKUP_DIR": str(self.dest), "AUU_BACKUP_APP": str(API)}):
            self.m = _load()

    def tearDown(self):
        self.tmp.cleanup()

    def _remote(self, names):
        p = subprocess.run([sys.executable, "-", str(self.data), ",".join(names)], input=self.m.REMOTE, capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        return json.loads(p.stdout.strip().splitlines()[-1])

    def _snapshot(self, ts="20261004-093000"):
        """What backup() builds, minus the SSH hop: remote script output -> gz + manifest."""
        res = self._remote(list(self.m.FILES))
        work = self.dest / ts
        work.mkdir(parents=True)
        man = {"files": {}}
        for name, meta in res["files"].items():
            if meta.get("missing"):
                man["files"][name] = {"missing": True}
                continue
            gz = work / f"{name}.gz"
            with open(Path(res["dir"]) / name, "rb") as fi, gzip.open(gz, "wb") as fo:
                shutil.copyfileobj(fi, fo)
            man["files"][name] = {**meta, "gz": gz.name, "gz_sha256": self.m.sha256(gz)}
        shutil.rmtree(res["dir"])
        (work / "MANIFEST.json").write_text(json.dumps(man))
        return work

    def test_remote_snapshot_is_consistent_under_wal(self):
        res = self._remote(["shadow_s3.sqlite", "mainstream_strategy.sqlite", "auth_log.sqlite"])
        try:
            f = res["files"]["shadow_s3.sqlite"]
            self.assertEqual(f["integrity"], "ok")
            self.assertEqual(f["counts"]["t"], 50)  # WAL content included by the backup API
            self.assertTrue(res["files"]["auth_log.sqlite"]["missing"])
            self.assertEqual(len(f["sha256"]), 64)
            self.assertTrue(Path(res["dir"]).name.startswith("auu-bk-"))
        finally:
            shutil.rmtree(res["dir"], ignore_errors=True)

    def test_verify_and_restore_drill(self):
        snap = self._snapshot()
        self.assertTrue(self.m.verify(snap)["ok"])
        r = self.m.restore_test(snap)
        self.assertTrue(r["ok"], r)
        self.assertTrue(r["app_read"]["ok"])
        self.assertEqual(r["app_read"]["runs"], 0)
        self.assertTrue(all(f["counts_match"] for f in r["files"].values()))

    def test_tampered_snapshot_is_detected(self):
        snap = self._snapshot()
        gz = snap / "shadow_s3.sqlite.gz"
        raw = bytearray(gz.read_bytes())
        raw[-12] ^= 0xFF
        gz.write_bytes(bytes(raw))
        v = self.m.verify(snap)
        self.assertFalse(v["ok"])
        self.assertIn("shadow_s3.sqlite.gz gz sha256", v["bad"])
        self.assertFalse(self.m.restore_test(snap)["ok"])

    def test_retention_keeps_30_days(self):
        old = (datetime.now(self.m.BJ) - timedelta(days=31)).strftime("%Y%m%d-%H%M%S")
        recent = (datetime.now(self.m.BJ) - timedelta(days=29)).strftime("%Y%m%d-%H%M%S")
        for ts in (old, recent):
            (self.dest / ts).mkdir(parents=True)
            (self.dest / ts / "MANIFEST.json").write_text("{}")
        gone = self.m.prune()
        self.assertEqual(gone, [old])
        self.assertTrue((self.dest / recent).exists())

    def test_no_secrets_in_file_list(self):
        self.assertNotIn("users.json", self.m.FILES)
        self.assertTrue(all(f.endswith(".sqlite") for f in self.m.FILES))
        self.assertIn("mainstream_strategy.sqlite", self.m.REQUIRED)


if __name__ == "__main__":
    unittest.main()
