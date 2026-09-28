"""Test stores stay off apps/api/data, and the repo .env stays unloaded.

Discovery sorts modules by name, so this file runs after the rest of the
suite. The snapshot was taken when isolation installed, before any test body.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

from app.data_paths import (
    DataDirGuardError,
    data_dir,
    repo_data_dir,
    repo_data_snapshot,
    snapshot_repo_data,
)
from app.paper.ledger import _journal_path, reset_paper_ledger
from app.paper.shadow_compare import _store_path, reset_shadow_compare


class RepoDataIsolationTests(unittest.TestCase):
    def test_defaults_point_at_temp_dir(self):
        self.assertEqual(os.environ.get("AUU_SKIP_DOTENV"), "1")
        self.assertEqual(os.environ.get("AUU_TEST"), "1")
        root = data_dir().resolve()
        repo = repo_data_dir().resolve()
        self.assertNotEqual(root, repo)
        self.assertFalse(str(root).startswith(str(repo) + os.sep))
        self.assertTrue(root.is_dir())
        self.assertTrue(str(root).startswith(str(Path(os.environ["AUU_DATA_DIR"]).resolve())))

        previous_journal = os.environ.get("PAPER_JOURNAL_STORE")
        previous_shadow = os.environ.get("SHADOW_COMPARE_STORE")
        os.environ.pop("PAPER_JOURNAL_STORE", None)
        os.environ.pop("SHADOW_COMPARE_STORE", None)
        try:
            journal = _journal_path().resolve()
            shadow = _store_path().resolve()
        finally:
            if previous_journal is None:
                os.environ.pop("PAPER_JOURNAL_STORE", None)
            else:
                os.environ["PAPER_JOURNAL_STORE"] = previous_journal
            if previous_shadow is None:
                os.environ.pop("SHADOW_COMPARE_STORE", None)
            else:
                os.environ["SHADOW_COMPARE_STORE"] = previous_shadow
        self.assertEqual(journal.parent, root)
        self.assertEqual(journal.name, "paper_journal.json")
        self.assertEqual(shadow.parent, root)
        self.assertEqual(shadow.name, "shadow_compare.json")

    def test_guard_blocks_repo_data_write(self):
        target = repo_data_dir() / "paper_journal.json"
        before = snapshot_repo_data()
        previous = os.environ.get("PAPER_JOURNAL_STORE")
        os.environ["PAPER_JOURNAL_STORE"] = str(target)
        try:
            with self.assertRaises(DataDirGuardError):
                reset_paper_ledger(wipe_store=True)
        finally:
            if previous is None:
                os.environ.pop("PAPER_JOURNAL_STORE", None)
            else:
                os.environ["PAPER_JOURNAL_STORE"] = previous
        self.assertEqual(snapshot_repo_data(), before)
        reset_paper_ledger(wipe_store=True)
        reset_shadow_compare()
        self.assertEqual(snapshot_repo_data(), before)

    def test_skip_dotenv(self):
        api_root = Path(__file__).resolve().parents[1]
        env_path = api_root / ".env"
        original = env_path.read_bytes() if env_path.exists() else None
        probe = "AUU_DOTENV_PROBE_9456"
        addition = f"{probe}=from-dotenv\nAUTO_PAPER_ORDERS=true\n".encode()
        try:
            if original is None:
                env_path.write_bytes(addition)
            else:
                env_path.write_bytes(original.rstrip() + b"\n" + addition)
            child_env = os.environ.copy()
            child_env.pop(probe, None)
            child_env.pop("AUTO_PAPER_ORDERS", None)
            child_env["AUU_SKIP_DOTENV"] = "1"
            child_env["AUU_TEST"] = "1"
            proc = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import json, os\n"
                    "import app.main\n"
                    "print(json.dumps({"
                    f"'probe': os.environ.get({probe!r}), "
                    "'auto': os.environ.get('AUTO_PAPER_ORDERS')"
                    "}))",
                ],
                cwd=api_root,
                env=child_env,
                capture_output=True,
                text=True,
                check=False,
            )
        finally:
            if original is None:
                env_path.unlink(missing_ok=True)
            else:
                env_path.write_bytes(original)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertIsNone(payload["probe"])
        self.assertIsNone(payload["auto"])

    def test_zzz_repo_data_unchanged_after_suite(self):
        self.assertEqual(snapshot_repo_data(), repo_data_snapshot())
