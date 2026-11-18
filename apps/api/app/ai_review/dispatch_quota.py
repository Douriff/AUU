"""Persistent daily cap on upstream dispatch attempts.

One row is one request this process is about to hand to the relay. The count
lives in its own SQLite file, not in a strategy or account ledger. A restart
reads the same file. The row stores a hash and an outcome code only.
"""
from __future__ import annotations

import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

from app.data_paths import DataDirGuardError, data_dir, guarded_path

_KEY = re.compile(r"^[0-9a-f]{64}$")
_OUTCOMES = {
    "reserved",
    "sending",
    "timeout",
    "bad_json",
    "output_limit",
    "upstream_error",
    "connection_error",
    "unverified",
    "not_object",
    "wrong_keys",
    "bad_summary",
    "summary_empty",
    "summary_too_long",
    "control_char",
    "injection",
    "number_not_in_facts",
    "citations_missing",
    "citation_shape",
    "citation_mismatch",
    "accepted",
}


def quota_path() -> Path:
    override = os.getenv("AUU_AI_RELAY_QUOTA_PATH", "").strip()
    path = Path(override).expanduser() if override else data_dir() / "ai_relay_dispatch.sqlite"
    return guarded_path(path)


def attempts_today(day: str) -> int:
    path = _existing_path()
    if path is None:
        return 0
    con = _connect(path, create=False)
    if con is None:
        return 0
    try:
        row = con.execute("SELECT attempts FROM day_attempts WHERE day=?", (day,)).fetchone()
    finally:
        con.close()
    return int(row[0]) if row else 0


def lookup(fact_key: str) -> Optional[str]:
    if _KEY.fullmatch(fact_key or "") is None:
        return None
    path = _existing_path()
    if path is None:
        return None
    con = _connect(path, create=False)
    if con is None:
        return None
    try:
        row = con.execute("SELECT outcome FROM dispatch WHERE fact_key=?", (fact_key,)).fetchone()
    finally:
        con.close()
    return None if row is None else str(row[0])


def reserve(fact_key: str, day: str, limit: Optional[int]) -> dict[str, Any]:
    """Claim one attempt, or refuse without changing the count.

    ``limit is None`` means the operator set ``unlimited``. There is still one
    row per fact hash, and the day's count is still stored.
    """
    if _KEY.fullmatch(fact_key or "") is None:
        return {"ok": False, "reason": "bad_key", "used": attempts_today(day)}
    if limit is not None and limit <= 0:
        return {"ok": False, "reason": "budget", "used": attempts_today(day)}
    con = _connect(quota_path(), create=True)
    assert con is not None
    try:
        con.execute("BEGIN IMMEDIATE")
        existing = con.execute("SELECT outcome FROM dispatch WHERE fact_key=?", (fact_key,)).fetchone()
        if existing is not None:
            used = _used(con, day)
            con.execute("COMMIT")
            return {"ok": False, "reason": "duplicate", "outcome": str(existing[0]), "used": used}
        used = _used(con, day)
        if limit is not None and used >= limit:
            con.execute("COMMIT")
            return {"ok": False, "reason": "budget", "used": used}
        if used == 0:
            con.execute("INSERT INTO day_attempts(day, attempts) VALUES (?, 1)", (day,))
        else:
            con.execute("UPDATE day_attempts SET attempts = attempts + 1 WHERE day=?", (day,))
        con.execute(
            "INSERT INTO dispatch(fact_key, day, outcome, created_at) VALUES (?, ?, 'reserved', ?)",
            (fact_key, day, int(time.time())),
        )
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    finally:
        con.close()
    return {"ok": True, "reason": None, "used": used + 1}


def begin_send(fact_key: str) -> bool:
    """Move a reservation to sending. A second caller loses and does not send."""
    if _KEY.fullmatch(fact_key or "") is None:
        return False
    path = _existing_path()
    if path is None:
        return False
    con = _connect(path, create=False)
    if con is None:
        return False
    try:
        con.execute("BEGIN IMMEDIATE")
        cur = con.execute(
            "UPDATE dispatch SET outcome='sending' WHERE fact_key=? AND outcome='reserved'",
            (fact_key,),
        )
        con.execute("COMMIT")
        return cur.rowcount == 1
    except Exception:
        con.execute("ROLLBACK")
        raise
    finally:
        con.close()


def mark(fact_key: str, outcome: str) -> None:
    if outcome not in _OUTCOMES or _KEY.fullmatch(fact_key or "") is None:
        return
    path = _existing_path()
    if path is None:
        return
    con = _connect(path, create=False)
    if con is None:
        return
    try:
        con.execute("UPDATE dispatch SET outcome=? WHERE fact_key=?", (outcome, fact_key))
        con.commit()
    finally:
        con.close()


def release_unsent(fact_key: str) -> bool:
    """Drop a reservation that never reached sending. A sent attempt stays."""
    if _KEY.fullmatch(fact_key or "") is None:
        return False
    path = _existing_path()
    if path is None:
        return False
    con = _connect(path, create=False)
    if con is None:
        return False
    try:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT day, outcome FROM dispatch WHERE fact_key=?", (fact_key,)).fetchone()
        if row is None or str(row[1]) != "reserved":
            con.execute("COMMIT")
            return False
        con.execute("DELETE FROM dispatch WHERE fact_key=? AND outcome='reserved'", (fact_key,))
        con.execute(
            "UPDATE day_attempts SET attempts = attempts - 1 WHERE day=? AND attempts > 0",
            (str(row[0]),),
        )
        con.execute("COMMIT")
        return True
    except Exception:
        con.execute("ROLLBACK")
        raise
    finally:
        con.close()


def reset_store() -> None:
    path = _configured_path()
    if path is None:
        return
    for suffix in ("", "-wal", "-shm"):
        file = Path(str(path) + suffix) if suffix else path
        if file.exists():
            file.unlink()


def _configured_path() -> Optional[Path]:
    override = os.getenv("AUU_AI_RELAY_QUOTA_PATH", "").strip()
    path = Path(override).expanduser() if override else data_dir() / "ai_relay_dispatch.sqlite"
    try:
        return guarded_path(path)
    except DataDirGuardError:
        return None


def _existing_path() -> Optional[Path]:
    path = _configured_path()
    if path is None or not path.exists():
        return None
    return path


def _connect(path: Path, *, create: bool) -> Optional[sqlite3.Connection]:
    if not create and not path.exists():
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=5, isolation_level=None)
    con.execute("PRAGMA busy_timeout=5000")
    con.execute("PRAGMA journal_mode=WAL")
    con.execute(
        "CREATE TABLE IF NOT EXISTS day_attempts (day TEXT PRIMARY KEY, attempts INTEGER NOT NULL)"
    )
    con.execute(
        "CREATE TABLE IF NOT EXISTS dispatch ("
        "fact_key TEXT PRIMARY KEY, day TEXT NOT NULL, outcome TEXT NOT NULL, created_at INTEGER NOT NULL)"
    )
    return con


def _used(con: sqlite3.Connection, day: str) -> int:
    row = con.execute("SELECT attempts FROM day_attempts WHERE day=?", (day,)).fetchone()
    return int(row[0]) if row else 0
