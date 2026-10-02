"""Data-directory defaults and the test write guard.

Production keeps using ``apps/api/data``. Isolation runs only for a
unittest/pytest process entry, or when ``AUU_TEST=1`` is already set.
It points ``AUU_DATA_DIR`` at a temporary directory, sets
``AUU_SKIP_DOTENV=1``, and raises ``DataDirGuardError`` before a write or
unlink inside the repository data directory.
"""
from __future__ import annotations

import hashlib
import os
import sys
import tempfile
from pathlib import Path

_REPO_DATA = Path(__file__).resolve().parents[1] / "data"
_SNAPSHOT: tuple[tuple[str, str], ...] | None = None
_TMPDIR: tempfile.TemporaryDirectory[str] | None = None

_STORE_ENVS = (
    "PAPER_JOURNAL_STORE",
    "SHADOW_COMPARE_STORE",
    "PAPER_EVIDENCE_STORE",
    "AUU_USER_STORE",
    "AUU_USER_JOURNAL_DIR",
    "AUU_WALLET_STORE",
    "TRADER_WATCH_STORE",
    "TRADER_DISTILL_OVERLAY",
)


class DataDirGuardError(RuntimeError):
    """A test process tried to write the repository data directory."""


def repo_data_dir() -> Path:
    return _REPO_DATA


def _truthy_env(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "on", "yes"}


def tests_active() -> bool:
    return _truthy_env("AUU_TEST")


def _module_invocation(tokens: list[str]) -> bool:
    for index, token in enumerate(tokens[:-1]):
        if token == "-m" and tokens[index + 1] in {"unittest", "pytest"}:
            return True
    return False


def argv_requests_test_runner(argv: list[str]) -> bool:
    """True when argv is a unittest or pytest process entry.

    A library that merely imports ``unittest`` or ``unittest.mock`` does not
    match. Stock CPython sets ``argv[0]`` to ``unittest/__main__.py``. Some
    wrappers store the command in ``argv[0]`` as ``python3 -m unittest``.
    """
    if not argv:
        return False
    head = argv[0].replace("\\", "/")
    if head.endswith("unittest/__main__.py") or head.endswith("pytest/__main__.py"):
        return True
    if head.endswith("/pytest") or head.endswith("/pytest.exe") or head in {"pytest", "pytest.exe"}:
        return True
    if _module_invocation(argv):
        return True
    return _module_invocation(head.split())


def test_process_requested(argv: list[str] | None = None) -> bool:
    """Explicit ``AUU_TEST=1``, or a unittest/pytest process entry."""
    if tests_active():
        return True
    return argv_requests_test_runner(sys.argv if argv is None else argv)


def data_dir() -> Path:
    raw = (os.getenv("AUU_DATA_DIR") or "").strip()
    if raw:
        return Path(raw).expanduser()
    return _REPO_DATA


def _inside_repo_data(path: Path) -> bool:
    try:
        resolved = path.expanduser().resolve()
    except OSError:
        return False
    repo = _REPO_DATA.resolve()
    if resolved == repo:
        return True
    try:
        resolved.relative_to(repo)
    except ValueError:
        return False
    return True


def guarded_path(path: Path) -> Path:
    """Return ``path``. During tests, refuse anything under ``apps/api/data``."""
    if tests_active() and _inside_repo_data(path):
        resolved = path.expanduser().resolve()
        raise DataDirGuardError(
            f"refusing to write repository data from tests: {resolved}"
        )
    return path


def snapshot_repo_data() -> tuple[tuple[str, str], ...]:
    """Content hash, size, and mtime of every file under the repo data dir."""
    root = _REPO_DATA
    if not root.exists():
        return (("__absent__", "missing"),)
    rows: list[tuple[str, str]] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames.sort()
        filenames.sort()
        base = Path(dirpath)
        for name in dirnames:
            rel = (base / name).relative_to(root).as_posix() + "/"
            rows.append((rel, "dir"))
        for name in filenames:
            file_path = base / name
            rel = file_path.relative_to(root).as_posix()
            try:
                payload = file_path.read_bytes()
                st = file_path.stat()
            except OSError as exc:
                rows.append((rel, f"unreadable:{exc.__class__.__name__}"))
                continue
            digest = hashlib.sha256(payload).hexdigest()
            rows.append((rel, f"{digest}:{st.st_size}:{st.st_mtime_ns}"))
    return tuple(rows)


def repo_data_snapshot() -> tuple[tuple[str, str], ...]:
    """Snapshot taken when test isolation was installed, before any test writes."""
    if _SNAPSHOT is None:
        return snapshot_repo_data()
    return _SNAPSHOT


def install_test_isolation() -> None:
    """Point test stores at a temp dir and skip the repository ``.env``.

    Idempotent. The temp directory lives for the process. Store env vars that
    already point inside ``apps/api/data`` are cleared so the temp default is
    used; a later explicit path outside the repo is still honored.
    """
    global _SNAPSHOT, _TMPDIR
    if _SNAPSHOT is None:
        _SNAPSHOT = snapshot_repo_data()
    os.environ["AUU_SKIP_DOTENV"] = "1"
    os.environ["AUU_TEST"] = "1"
    # The legacy pump suite exercises app.legacy.pump; mainstream tests build
    # their own app with create_app(legacy=False). No background CEX refresh.
    os.environ.setdefault("AUU_LEGACY_PUMP", "on")
    os.environ.setdefault("AUU_MAINSTREAM_REFRESH", "off")
    raw_root = (os.getenv("AUU_DATA_DIR") or "").strip()
    if not raw_root or _inside_repo_data(Path(raw_root)):
        if _TMPDIR is None:
            _TMPDIR = tempfile.TemporaryDirectory(prefix="auu-test-data-")
        os.environ["AUU_DATA_DIR"] = _TMPDIR.name
    for key in _STORE_ENVS:
        raw = (os.getenv(key) or "").strip()
        if raw and _inside_repo_data(Path(raw)):
            os.environ.pop(key, None)
