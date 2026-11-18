#!/usr/bin/env python3
"""Fixed remote helper for the restricted backup account.

The box installs this as root:root, mode 0755, at ``/usr/local/sbin/auu_remote_snapshot.py``.
``auubackup`` may sudo only ``snapshot`` as the ``auu`` user. ``rev`` reads ``/opt/auu``
and does not need sudo. There is no path argument: a stolen key cannot point this
program at ``users.json`` or ``auu.env``.

``snapshot`` opens the allowlisted SQLite files as ``auu`` (so WAL works) and writes
copies under ``/var/backups/auu-stage``. It does not print or copy user records,
session secrets, or API keys.
"""
from __future__ import annotations

import grp
import hashlib
import json
import os
import sqlite3
import stat
import subprocess
import sys
import tempfile

DATA = "/var/lib/auu/data"
REPO = "/opt/auu"
STAGE = "/var/backups/auu-stage"
GROUP = "auubackup"
FILES = [
    "mainstream_strategy.sqlite",
    "shadow_s3.sqlite",
    "mainstream_paper.sqlite",
    "alerts.sqlite",
    "recon.sqlite",
    "auth_log.sqlite",
    "mainstream.sqlite",
    "price_alerts.sqlite",
    "chart_drawings.sqlite",
    "airdrops.sqlite",
]


def _die(msg: str, code: int = 2) -> None:
    print(msg, file=sys.stderr)
    raise SystemExit(code)


def _hex_rev(token: str) -> str:
    raw = token.strip().lower()
    if 7 <= len(raw) <= 64 and set(raw) <= set("0123456789abcdef"):
        return raw
    return ""


def read_server_rev(repo: str) -> tuple[str, str]:
    """Return ``(source, rev)``. Source is ``git``, ``commit``, or ``missing``.

    A checkout with ``.git`` keeps ``git rev-parse --short HEAD``. A release tree
    with no ``.git`` uses the first hex token in ``COMMIT``. Anything else is
    missing; this never substitutes a timestamp or a made-up hash.
    """
    root = os.path.abspath(repo)
    if os.path.exists(os.path.join(root, ".git")):
        proc = subprocess.run(
            ["git", "-C", root, "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True,
        )
        rev = _hex_rev(proc.stdout.strip())
        if proc.returncode == 0 and rev:
            return "git", rev
    commit = os.path.join(root, "COMMIT")
    if os.path.isfile(commit):
        try:
            with open(commit, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            text = ""
        token = text.split()[0] if text.split() else ""
        rev = _hex_rev(token)
        if rev:
            return "commit", rev
    return "missing", ""


def format_rev(source: str, rev: str) -> str:
    if not rev:
        return "missing"
    return f"{source} {rev}"


def assert_stage(path: str, group: str = GROUP) -> None:
    try:
        st = os.stat(path)
    except OSError as exc:
        _die(f"backup stage missing ({exc.__class__.__name__})")
    if not stat.S_ISDIR(st.st_mode):
        _die("backup stage is not a directory")
    mode = stat.S_IMODE(st.st_mode)
    if mode & 0o002:
        _die("backup stage must not be world-writable")
    if not mode & 0o020:
        _die("backup stage must be group-writable by the backup group")
    try:
        name = grp.getgrgid(st.st_gid).gr_name
    except KeyError:
        _die("backup stage group is unknown")
    if name != group:
        _die(f"backup stage group is {name}")


def snapshot_dir(data: str, names: list[str], parent: str, share_group: str | None) -> dict:
    if share_group:
        assert_stage(parent, share_group)
    out = tempfile.mkdtemp(prefix="auu-bk-", dir=parent)
    os.chmod(out, 0o700)
    res: dict = {"dir": out, "files": {}}
    for name in names:
        if name != os.path.basename(name) or name.startswith("."):
            _die(f"refusing file name {name}")
        src = os.path.join(data, name)
        if not os.path.isfile(src):
            res["files"][name] = {"missing": True}
            continue
        dst = os.path.join(out, name)
        src_db = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=30)
        try:
            dst_db = sqlite3.connect(dst)
            try:
                src_db.backup(dst_db)
                chk = dst_db.execute("PRAGMA integrity_check").fetchone()[0]
                tables = [row[0] for row in dst_db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
                counts = {table: dst_db.execute('SELECT COUNT(*) FROM "%s"' % table.replace('"', '""')).fetchone()[0]
                          for table in tables}
            finally:
                dst_db.close()
        finally:
            src_db.close()
        with open(dst, "rb") as fh:
            digest = hashlib.sha256(fh.read()).hexdigest()
        res["files"][name] = {"sha256": digest, "bytes": os.path.getsize(dst), "integrity": chk, "counts": counts}
    if share_group:
        gid = grp.getgrnam(share_group).gr_gid
        os.chown(out, -1, gid)
        os.chmod(out, 0o2770)
        for name, meta in res["files"].items():
            if meta.get("missing"):
                continue
            path = os.path.join(out, name)
            os.chown(path, -1, gid)
            os.chmod(path, 0o640)
    return res


def main(argv: list[str]) -> int:
    if argv == ["snapshot"]:
        print(json.dumps(snapshot_dir(DATA, FILES, STAGE, GROUP)))
        return 0
    if argv == ["rev"]:
        source, rev = read_server_rev(REPO)
        print(format_rev(source, rev))
        return 0
    print("usage: auu_remote_snapshot.py snapshot|rev", file=sys.stderr)
    return 2


# --- cli ---
if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
