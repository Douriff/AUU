"""Identity bundle encrypts outside the repo and verify does not print secrets."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "ops" / "backup" / "identity_backup.py"
PASSPHRASE = "test-passphrase-16"


class IdentityBackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        users = self.root / "users.json"
        users.write_text(json.dumps({"users": [{"id": "abc", "name": "secret-name", "password_hash": "hashed"}]}), encoding="utf-8")
        self.env = {
            **os.environ,
            "AUU_USER_STORE": str(users),
            "AUU_SESSION_SECRET": "server-secret-not-for-git",
            "AUU_IDENTITY_BACKUP_PASSPHRASE": PASSPHRASE,
        }
        self.out = self.root / "bundle.bin"

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, *args, env=None):
        return subprocess.run([sys.executable, str(SCRIPT), *args], cwd=self.root, env=env or self.env,
                              text=True, capture_output=True, check=False)

    def test_roundtrip_hides_contents_and_rejects_repo_and_tamper(self):
        made = self._run("backup", str(self.out))
        self.assertEqual(made.returncode, 0, made.stderr)
        self.assertIn("ok users=1", made.stdout)
        self.assertNotIn("secret-name", made.stdout + made.stderr)
        self.assertNotIn("server-secret", made.stdout + made.stderr)
        self.assertNotIn(PASSPHRASE, made.stdout + made.stderr)
        checked = self._run("verify", str(self.out))
        self.assertEqual(checked.returncode, 0, checked.stderr)
        self.assertEqual(checked.stdout.strip(), "ok users=1")
        self.assertNotIn("secret-name", checked.stdout + checked.stderr)
        blob = bytearray(self.out.read_bytes())
        blob[-1] ^= 0xFF
        self.out.write_bytes(blob)
        bad = self._run("verify", str(self.out))
        self.assertNotEqual(bad.returncode, 0)
        self.assertNotIn("secret-name", bad.stdout + bad.stderr)
        inside = self._run("backup", str(ROOT / "identity-do-not-commit.bin"))
        self.assertNotEqual(inside.returncode, 0)
        self.assertFalse((ROOT / "identity-do-not-commit.bin").exists())
        short = dict(self.env)
        short["AUU_IDENTITY_BACKUP_PASSPHRASE"] = "too-short"
        self.assertNotEqual(self._run("backup", str(self.root / "nope.bin"), env=short).returncode, 0)
