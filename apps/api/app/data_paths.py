"""Data-directory defaults and the test write guard.

Production keeps using ``apps/api/data``. A unittest process calls
``install_test_isolation()`` first: ``AUU_DATA_DIR`` points at a temporary
directory, ``AUU_SKIP_DOTENV=1``, and any store path that resolves inside
the repository data directory raises ``DataDirGuardError`` before the
write or unlink.
"""
from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path

_REPO_DATA = Path(__file__).resolve().parents[1] / "data"
_SNAPSHOT: tuple[tuple[str, str], ...] | None = None
_TMPDIR: tempfile.TemporaryDirectory[str] | None = None

_STORE_ENVS = (
    "PAPER_JOURNAL_STORE",
    "SHADOW_COMPARE_STORE",
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


def tests_active() -> bool:
    return os.getenv("AUU_TEST", "").strip().lower() in {"1", "true", "on", "yes"}


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
    raw_root = (os.getenv("AUU_DATA_DIR") or "").strip()
    if not raw_root or _inside_repo_data(Path(raw_root)):
        if _TMPDIR is None:
            _TMPDIR = tempfile.TemporaryDirectory(prefix="auu-test-data-")
        os.environ["AUU_DATA_DIR"] = _TMPDIR.name
    for key in _STORE_ENVS:
        raw = (os.getenv(key) or "").strip()
        if raw and _inside_repo_data(Path(raw)):
            os.environ.pop(key, None)
