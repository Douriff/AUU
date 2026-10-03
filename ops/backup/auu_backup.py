#!/usr/bin/env python3
"""Offsite ledger backup (P1-7): pull consistent SQLite snapshots from the AUU server to the box.

Runs on the box (initiated from here with the existing SSH key; nothing is installed or stored on the
server). On the server, as the ``auu`` user, each database is copied with the SQLite online backup API
(consistent under WAL while the API runs) into a private temp dir, checked with ``PRAGMA integrity_check``
and hashed (SHA-256). The box pulls the copies over SFTP, gzips them into
``/workspace/backups/auu/<YYYYmmdd-HHMMSS>/``, verifies every hash, writes ``MANIFEST.json`` +
``SHA256SUMS``, runs a restore drill (decompress → integrity_check → row counts → open with the app's
ledger code) and prunes snapshots older than 30 days. The server temp dir is removed afterwards.

Usage: auu_backup.py backup | verify [DIR] | restore-test [DIR] | list
No exchange keys, SMTP passwords, session secrets or users.json are copied.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

BJ = timezone(timedelta(hours=8))
DEST = Path(os.environ.get("AUU_BACKUP_DIR", "/workspace/backups/auu"))
KEEP_DAYS = int(os.environ.get("AUU_BACKUP_KEEP_DAYS", "30"))
HOST = os.environ.get("AUU_BACKUP_HOST", "156.227.238.242")
SSH_USER = "root"
KEY = os.environ.get("AUU_BACKUP_KEY", "/workspace/.ssh_auu/auu_server")
KNOWN_HOSTS = os.environ.get("AUU_BACKUP_KNOWN_HOSTS", "/workspace/.ssh_auu/known_hosts")
DATA = "/var/lib/auu/data"
APP_API = os.environ.get("AUU_BACKUP_APP", "/workspace/AUU/apps/api")
# Key SQLite files (ledgers + evidence). mainstream.sqlite = market data the strategy read (re-fetchable but kept for audit).
FILES = [
    "mainstream_strategy.sqlite",
    "shadow_s3.sqlite",
    "mainstream_paper.sqlite",
    "alerts.sqlite",
    "recon.sqlite",
    "auth_log.sqlite",
    "mainstream.sqlite",
]
REQUIRED = {"mainstream_strategy.sqlite", "shadow_s3.sqlite", "mainstream_paper.sqlite"}

# Executed on the server by `runuser -u auu -- python3 -` (stdin); prints one JSON line.
REMOTE = r'''
import hashlib, json, os, sqlite3, sys, tempfile
data, names = sys.argv[1], sys.argv[2].split(",")
out = tempfile.mkdtemp(prefix="auu-bk-", dir="/tmp")
os.chmod(out, 0o700)
res = {"dir": out, "files": {}}
for n in names:
    src = os.path.join(data, n)
    if not os.path.exists(src):
        res["files"][n] = {"missing": True}
        continue
    dst = os.path.join(out, n)
    s = sqlite3.connect("file:%s?mode=ro" % src, uri=True, timeout=30)
    d = sqlite3.connect(dst)
    s.backup(d)
    s.close()
    chk = d.execute("PRAGMA integrity_check").fetchone()[0]
    tables = [r[0] for r in d.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    counts = {t: d.execute('SELECT COUNT(*) FROM "%s"' % t.replace('"', '""')).fetchone()[0] for t in tables}
    d.close()
    h = hashlib.sha256(open(dst, "rb").read()).hexdigest()
    res["files"][n] = {"sha256": h, "bytes": os.path.getsize(dst), "integrity": chk, "counts": counts}
print(json.dumps(res))
'''


def log(msg: str) -> None:
    print(f"{datetime.now(BJ):%Y-%m-%d %H:%M:%S} {msg}", flush=True)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def connect():
    import paramiko

    c = paramiko.SSHClient()
    c.load_host_keys(KNOWN_HOSTS)  # pinned host key; never auto-accept
    c.set_missing_host_key_policy(paramiko.RejectPolicy())
    c.connect(HOST, username=SSH_USER, key_filename=KEY, timeout=20, allow_agent=False, look_for_keys=False)
    return c


def run(c, cmd: str, stdin: str = "", timeout: int = 600) -> tuple[int, str, str]:
    i, o, e = c.exec_command(cmd, timeout=timeout)
    if stdin:
        i.write(stdin)
    i.channel.shutdown_write()
    out, err = o.read().decode(errors="replace"), e.read().decode(errors="replace")
    return o.channel.recv_exit_status(), out, err


def backup() -> Path:
    ts = datetime.now(BJ).strftime("%Y%m%d-%H%M%S")
    DEST.mkdir(parents=True, exist_ok=True)
    work = DEST / f".partial-{ts}"
    work.mkdir()
    c = connect()
    remote_dir = None
    try:
        rc, out, err = run(c, f"runuser -u auu -- python3 - {DATA} {','.join(FILES)}", stdin=REMOTE)
        if rc != 0:
            raise RuntimeError(f"remote snapshot failed rc={rc}: {err[-400:]}")
        res = json.loads(out.strip().splitlines()[-1])
        remote_dir = res["dir"]
        if not remote_dir.startswith("/tmp/auu-bk-"):
            raise RuntimeError("unexpected remote dir")
        _, rev, _ = run(c, "cd /opt/auu && git rev-parse --short HEAD")
        sftp = c.open_sftp()
        manifest = {"created_bj": datetime.now(BJ).isoformat(timespec="seconds"), "host": HOST, "server_rev": rev.strip(),
                    "method": "sqlite3 online backup API (server, as auu) -> sftp -> gzip", "files": {}}
        for name, meta in res["files"].items():
            if meta.get("missing"):
                if name in REQUIRED:
                    raise RuntimeError(f"required file missing on server: {name}")
                manifest["files"][name] = {"missing": True}
                continue
            if meta["integrity"] != "ok":
                raise RuntimeError(f"integrity_check failed on server copy of {name}: {meta['integrity']}")
            raw = work / name
            sftp.get(f"{remote_dir}/{name}", str(raw))
            if sha256(raw) != meta["sha256"]:
                raise RuntimeError(f"sha256 mismatch after transfer: {name}")
            gz = work / f"{name}.gz"
            with open(raw, "rb") as fi, gzip.open(gz, "wb", compresslevel=6) as fo:
                shutil.copyfileobj(fi, fo)
            raw.unlink()
            manifest["files"][name] = {**meta, "gz": gz.name, "gz_sha256": sha256(gz), "gz_bytes": gz.stat().st_size}
        sftp.close()
    finally:
        if remote_dir and remote_dir.startswith("/tmp/auu-bk-"):
            run(c, f"rm -rf -- '{remote_dir}'")
        c.close()
    (work / "MANIFEST.json").write_text(json.dumps(manifest, indent=2, sort_keys=True))
    sums = [f"{m['sha256']}  {n}\n{m['gz_sha256']}  {m['gz']}" for n, m in sorted(manifest["files"].items()) if not m.get("missing")]
    (work / "SHA256SUMS").write_text("\n".join(sums) + "\n")
    final = DEST / ts
    work.rename(final)
    os.chmod(final, 0o700)
    log(f"backup {final.name}: " + ", ".join(f"{n} {m.get('bytes', 0) // 1024}KB" for n, m in manifest["files"].items() if not m.get("missing")))
    return final


def snapshots() -> list[Path]:
    return sorted(p for p in DEST.glob("2*") if p.is_dir() and (p / "MANIFEST.json").exists())


def latest() -> Path:
    s = snapshots()
    if not s:
        raise SystemExit("no snapshot")
    return s[-1]


def verify(d: Path) -> dict:
    """Re-check every gz hash and every decompressed DB hash against the manifest."""
    man = json.loads((d / "MANIFEST.json").read_text())
    bad = []
    for name, m in man["files"].items():
        if m.get("missing"):
            continue
        gz = d / m["gz"]
        if sha256(gz) != m["gz_sha256"]:
            bad.append(f"{m['gz']} gz sha256")
            continue
        h = hashlib.sha256()
        with gzip.open(gz, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        if h.hexdigest() != m["sha256"]:
            bad.append(f"{name} sha256")
    return {"snapshot": d.name, "ok": not bad, "bad": bad, "files": len([1 for m in man["files"].values() if not m.get("missing")])}


def restore_test(d: Path) -> dict:
    """Restore drill: decompress into a temp dir, integrity_check, compare row counts, open with the app's ledger code."""
    man = json.loads((d / "MANIFEST.json").read_text())
    v = verify(d)
    out = {"snapshot": d.name, "hashes_ok": v["ok"], "files": {}}
    if not v["ok"]:  # never restore from a snapshot whose hashes do not match
        return {**out, "ok": False, "bad": v["bad"], "app_read": {"ok": False, "error": "hash mismatch"}}
    with tempfile.TemporaryDirectory(prefix="auu-restore-") as tmp:
        for name, m in man["files"].items():
            if m.get("missing"):
                continue
            dst = Path(tmp) / name
            with gzip.open(d / m["gz"], "rb") as fi, open(dst, "wb") as fo:
                shutil.copyfileobj(fi, fo)
            con = sqlite3.connect(dst)
            chk = con.execute("PRAGMA integrity_check").fetchone()[0]
            counts = {t: con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in m["counts"]}
            con.close()
            out["files"][name] = {"integrity": chk, "counts_match": counts == m["counts"]}
        out["app_read"] = _app_read(Path(tmp))
    out["ok"] = bool(out["hashes_ok"] and all(f["integrity"] == "ok" and f["counts_match"] for f in out["files"].values())
                     and out["app_read"].get("ok"))
    return out


def _app_read(tmp: Path) -> dict:
    """Open the restored strategy ledger with the application's own reader (separate process, data dir = restore dir)."""
    py = Path(APP_API) / ".venv/bin/python"
    if not py.exists():
        return {"ok": False, "error": "app venv missing"}
    code = (
        "import json,sys\n"
        "from app.paper.strategy_runner import StrategyLedger\n"
        "led=StrategyLedger(sys.argv[1]+'/mainstream_strategy.sqlite')\n"
        "runs=led.runs(); last=runs[-1] if runs else None\n"
        "print(json.dumps({'runs':len(runs),'lastDay':(int(last['day']) if last else None),"
        "'nav':(float(last['nav_close']) if last else None),'fills':len(led.fills(100000))}))\n"
    )
    import subprocess

    env = {"HOME": os.environ.get("HOME", "/tmp"), "PATH": os.environ.get("PATH", "/usr/bin"), "AUU_SKIP_DOTENV": "1",
           "AUU_DATA_DIR": str(tmp), "PYTHONPATH": APP_API}
    p = subprocess.run([str(py), "-c", code, str(tmp)], cwd=APP_API, env=env, capture_output=True, text=True, timeout=120)
    if p.returncode != 0:
        return {"ok": False, "error": p.stderr.strip().splitlines()[-1][:200] if p.stderr.strip() else f"rc {p.returncode}"}
    r = json.loads(p.stdout.strip().splitlines()[-1])
    return {"ok": True, **r}


def prune() -> list[str]:
    cutoff = datetime.now(BJ) - timedelta(days=KEEP_DAYS)
    gone = []
    for p in snapshots():
        try:
            t = datetime.strptime(p.name, "%Y%m%d-%H%M%S").replace(tzinfo=BJ)
        except ValueError:
            continue
        if t < cutoff and p != snapshots()[-1]:
            shutil.rmtree(p)
            gone.append(p.name)
    for p in DEST.glob(".partial-*"):  # failed runs
        if time.time() - p.stat().st_mtime > 86400:
            shutil.rmtree(p, ignore_errors=True)
    return gone


def main(argv: list[str]) -> int:
    cmd = argv[0] if argv else "list"
    if cmd == "backup":
        d = backup()
        r = restore_test(d)
        (d / "RESTORE_TEST.json").write_text(json.dumps(r, indent=2))
        gone = prune()
        log(f"restore-test {'OK' if r['ok'] else 'FAILED'} {json.dumps(r['app_read'])}; pruned {gone or 'none'}")
        return 0 if r["ok"] else 1
    if cmd == "verify":
        r = verify(Path(argv[1]) if len(argv) > 1 else latest())
        print(json.dumps(r))
        return 0 if r["ok"] else 1
    if cmd == "restore-test":
        r = restore_test(Path(argv[1]) if len(argv) > 1 else latest())
        print(json.dumps(r, indent=2))
        return 0 if r["ok"] else 1
    if cmd == "list":
        for p in snapshots():
            print(p.name, sum(f.stat().st_size for f in p.iterdir()) // 1024, "KB")
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
