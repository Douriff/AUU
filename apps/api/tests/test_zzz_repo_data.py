"""Test stores stay off apps/api/data, and the repo .env stays unloaded.

Discovery sorts modules by name, so this file runs after the rest of the
suite. The snapshot was taken when isolation installed, before any test body.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from app.data_paths import (
    DataDirGuardError,
    argv_requests_test_runner,
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

    def test_argv_entry_is_narrow(self):
        self.assertTrue(
            argv_requests_test_runner(
                ["/usr/lib/python3.12/unittest/__main__.py", "discover", "-s", "tests"]
            )
        )
        self.assertTrue(argv_requests_test_runner(["/usr/bin/pytest"]))
        self.assertTrue(argv_requests_test_runner(["/opt/pytest/__main__.py"]))
        self.assertTrue(argv_requests_test_runner(["/usr/bin/python3", "-m", "unittest", "discover"]))
        self.assertTrue(argv_requests_test_runner(["python", "-m", "pytest"]))
        self.assertTrue(argv_requests_test_runner(["python3 -m unittest", "discover", "-s", "tests"]))
        self.assertTrue(argv_requests_test_runner(["python3 -m pytest"]))
        self.assertFalse(argv_requests_test_runner(["python3 -m uvicorn", "app.main:app"]))
        self.assertFalse(argv_requests_test_runner(["/usr/bin/uvicorn", "app.main:app"]))
        self.assertFalse(argv_requests_test_runner(["-c"]))
        self.assertFalse(argv_requests_test_runner(["/tmp/import_app.py"]))

    def test_unittest_mock_import_does_not_isolate(self):
        api_root = Path(__file__).resolve().parents[1]
        script = (
            "import json, os\n"
            "import unittest.mock\n"
            "import app.main\n"
            "from app.paper.ledger import _journal_path\n"
            "os.environ.pop('PAPER_JOURNAL_STORE', None)\n"
            "print(json.dumps({\n"
            "  'test': os.environ.get('AUU_TEST'),\n"
            "  'skip': os.environ.get('AUU_SKIP_DOTENV'),\n"
            "  'data': os.environ.get('AUU_DATA_DIR'),\n"
            "  'journal': str(_journal_path()),\n"
            "}))\n"
        )
        proc = self._python_child(script, api_root, extra_env={})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertIsNone(payload["test"])
        self.assertIsNone(payload["skip"])
        self.assertIsNone(payload["data"])
        journal = Path(payload["journal"]).resolve()
        self.assertEqual(journal, (repo_data_dir() / "paper_journal.json").resolve())
        self.assertNotIn("auu-test-data-", str(journal))

    def test_skip_dotenv(self):
        api_root = Path(__file__).resolve().parents[1]
        repo_env = api_root / ".env"
        before = self._env_stat(repo_env)
        probe = "AUU_DOTENV_PROBE_9456"
        with tempfile.TemporaryDirectory(prefix="auu-dotenv-") as tmp:
            env_path = Path(tmp) / "isolated.env"
            env_path.write_text(f"{probe}=from-dotenv\nAUTO_PAPER_ORDERS=true\n", encoding="utf-8")
            data_root = Path(tmp) / "data"
            data_root.mkdir()
            loaded = self._import_main(
                api_root,
                {
                    "AUU_DOTENV_PATH": str(env_path),
                    "AUU_DATA_DIR": str(data_root),
                },
            )
            skipped = self._import_main(
                api_root,
                {
                    "AUU_DOTENV_PATH": str(env_path),
                    "AUU_SKIP_DOTENV": "1",
                    "AUU_DATA_DIR": str(data_root),
                },
            )
        self.assertEqual(self._env_stat(repo_env), before)
        self.assertEqual(loaded["probe"], "from-dotenv")
        self.assertEqual(loaded["auto"], "true")
        self.assertIsNone(skipped["probe"])
        self.assertIsNone(skipped["auto"])

    def _import_main(self, api_root: Path, extra_env: dict[str, str]) -> dict[str, str | None]:
        script = (
            "import json, os\n"
            "import app.main\n"
            "print(json.dumps({\n"
            "  'probe': os.environ.get('AUU_DOTENV_PROBE_9456'),\n"
            "  'auto': os.environ.get('AUTO_PAPER_ORDERS'),\n"
            "}))\n"
        )
        proc = self._python_child(script, api_root, extra_env=extra_env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout.strip().splitlines()[-1])
        return payload

    def _python_child(self, script: str, api_root: Path, extra_env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        child_env = os.environ.copy()
        for key in (
            "AUU_TEST",
            "AUU_SKIP_DOTENV",
            "AUU_DATA_DIR",
            "AUU_DOTENV_PATH",
            "AUU_DOTENV_PROBE_9456",
            "AUTO_PAPER_ORDERS",
            "PAPER_JOURNAL_STORE",
        ):
            child_env.pop(key, None)
        child_env.update(extra_env)
        child_env["PYTHONPATH"] = os.pathsep.join(
            [str(api_root), child_env.get("PYTHONPATH", "")]
        ).rstrip(os.pathsep)
        with tempfile.TemporaryDirectory(prefix="auu-child-") as tmp:
            script_path = Path(tmp) / "import_app.py"
            script_path.write_text(script, encoding="utf-8")
            return subprocess.run(
                [sys.executable, str(script_path)],
                cwd=api_root,
                env=child_env,
                capture_output=True,
                text=True,
                check=False,
            )

    @staticmethod
    def _env_stat(path: Path) -> tuple[int, int] | None:
        if not path.exists():
            return None
        st = path.stat()
        return (st.st_size, st.st_mtime_ns)

    def test_zzz_repo_data_unchanged_after_suite(self):
        self.assertEqual(snapshot_repo_data(), repo_data_snapshot())
