#!/usr/bin/env python3
"""Offsite ledger backup (P1-7): pull consistent SQLite snapshots from the AUU server to the box.

Runs on the box (initiated from here with the existing SSH key; nothing is installed or stored on the
server). On the server, as the ``auu`` user, each database is copied with the SQLite online backup API
(consistent under WAL while the API runs) into a private temp dir, checked with ``PRAGMA integrity_check``
and hashed (SHA-256). The box pulls the copies over SFTP, gzips them into
``$AUU_BACKUP_DIR/<YYYYmmdd-HHMMSS>/``, verifies every hash, writes ``MANIFEST.json`` +
``SHA256SUMS``, runs a restore drill (decompress → integrity_check → row counts → open with the app's
ledger code) and prunes snapshots older than 30 days. The server temp dir is removed afterwards.

Usage: auu_backup.py backup | verify [DIR] | restore-test [DIR] | list
No exchange keys, SMTP passwords, session secrets or users.json are copied.
``AUU_BACKUP_PORT`` defaults to 22. Any other port is pinned as ``[host]:port`` and does not reuse a
bare known_hosts line. The recorded revision is ``git rev-parse --short HEAD`` when ``.git`` exists,
otherwise the hex token in ``COMMIT``; a missing file is left missing rather than replaced with a made-up id.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import shlex
import shutil
import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

BJ = timezone(timedelta(hours=8))
DEST = Path(os.environ.get("AUU_BACKUP_DIR") or Path.home() / "backups" / "auu")
KEEP_DAYS = int(os.environ.get("AUU_BACKUP_KEEP_DAYS", "30"))
# Host, key and pinned known_hosts come from the box-local config (auu_backup.env next to the installed script).
# Port, user and remote paths are read per run so an already-imported module still sees the scheduler env.
HOST = os.environ.get("AUU_BACKUP_HOST", "")
KEY = os.environ.get("AUU_BACKUP_KEY", "")
KNOWN_HOSTS = os.environ.get("AUU_BACKUP_KNOWN_HOSTS", "")
DATA = os.environ.get("AUU_BACKUP_REMOTE_DATA", "/var/lib/auu/data")
REMOTE_REPO = os.environ.get("AUU_BACKUP_REMOTE_REPO", "/opt/auu")
APP_API = os.environ.get("AUU_BACKUP_APP") or str(Path(__file__).resolve().parents[2] / "apps" / "api")
# Key SQLite files (ledgers + evidence). mainstream.sqlite = market data the strategy read (re-fetchable but kept for audit).
FILES = [
    "mainstream_strategy.sqlite",
    "shadow_s3.sqlite",
    "mainstream_paper.sqlite",
    "alerts.sqlite",
    "recon.sqlite",
    "auth_log.sqlite",
    "mainstream.sqlite",
    # user data without secrets: price alerts (+ in-site notifications), chart drawings, airdrop radar
    "price_alerts.sqlite",
    "chart_drawings.sqlite",
    "airdrops.sqlite",
]
# Deliberately NOT here: exchange.sqlite (encrypted user exchange API credentials), users.json, any key / env file.
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


def backup_port(raw: str | None = None) -> int:
    """``AUU_BACKUP_PORT``; empty means 22 so an old env file keeps the previous port."""
    if raw is None:
        raw = os.environ.get("AUU_BACKUP_PORT", "")
    text = (raw or "").strip()
    if text == "":
        return 22
    if not text.isdigit():
        raise SystemExit("AUU_BACKUP_PORT must be an integer from 1 to 65535")
    port = int(text)
    if not 1 <= port <= 65535:
        raise SystemExit("AUU_BACKUP_PORT must be an integer from 1 to 65535")
    return port


def backup_user() -> str:
    """``AUU_BACKUP_USER``; empty means root, which keeps the old runuser path."""
    user = os.environ.get("AUU_BACKUP_USER", "").strip() or "root"
    if user.startswith("-") or not user.replace("-", "").replace("_", "").isalnum():
        raise SystemExit("AUU_BACKUP_USER must be a plain account name")
    return user


def backup_host() -> str:
    host = os.environ.get("AUU_BACKUP_HOST", "").strip()
    if any(ch in host for ch in " \t\r\n'\"\\$[]"):
        raise SystemExit("AUU_BACKUP_HOST has illegal characters")
    return host


def hostkey_name(host: str, port: int) -> str:
    """OpenSSH known_hosts name. Port 22 is the bare host; any other port is ``[host]:port``."""
    if port == 22:
        return host
    return f"[{host}]:{port}"


def _remote_path(raw: str, label: str) -> str:
    path = raw.strip()
    if not path.startswith("/") or any(ch in path for ch in "\n\r\x00"):
        raise SystemExit(f"{label} must be an absolute path")
    return path


def remote_helper() -> str:
    return _remote_path(os.environ.get("AUU_BACKUP_REMOTE_HELPER", "/usr/local/sbin/auu_remote_snapshot.py"),
                        "AUU_BACKUP_REMOTE_HELPER")


def pinned_host_keys(path: str, host: str, port: int):
    """Load only the pinned file and require the exact host:port entry.

    A bare hostname line is not a match for a non-22 port, and system ``known_hosts``
    is not consulted. Missing or mismatched keys never get auto-accepted.
    """
    import paramiko

    name = hostkey_name(host, port)
    try:
        keys = paramiko.HostKeys(path)
    except OSError as exc:
        raise SystemExit(f"cannot read AUU_BACKUP_KNOWN_HOSTS ({exc.__class__.__name__})") from exc
    found = keys.lookup(name)
    if not found:
        raise SystemExit(f"known_hosts has no key for {name}; refusing to connect")
    return name


def root_rev_stdin() -> str:
    """Server-side rev program: the helper module without its CLI, plus one call.

    Root mode has no extra file on the server. Restricted mode runs the installed helper instead.
    """
    path = Path(__file__).with_name("auu_remote_snapshot.py")
    text = path.read_text(encoding="utf-8")
    marker = "# --- cli ---"
    if marker not in text:
        raise RuntimeError("auu_remote_snapshot.py is missing its cli marker")
    return text.split(marker, 1)[0] + (
        "import sys\n"
        "_src, _rev = read_server_rev(sys.argv[1])\n"
        "print('missing' if not _rev else f'{_src} {_rev}')\n"
    )


def parse_rev_output(text: str) -> tuple[str, str]:
    """Return ``(rev, source)``. Unrecognized text is missing, not a version string."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return "", "missing"
    parts = lines[-1].split()
    if parts == ["missing"]:
        return "", "missing"
    if len(parts) == 2 and parts[0] in {"git", "commit"}:
        rev = parts[1].strip().lower()
        if 7 <= len(rev) <= 64 and set(rev) <= set("0123456789abcdef"):
            return rev, parts[0]
    return "", "missing"


def remote_snapshot_invocation(user: str | None = None) -> tuple[str, str]:
    user = backup_user() if user is None else user
    if user == "root":
        data = _remote_path(os.environ.get("AUU_BACKUP_REMOTE_DATA", DATA), "AUU_BACKUP_REMOTE_DATA")
        cmd = "runuser -u auu -- python3 - " + shlex.quote(data) + " " + shlex.quote(",".join(FILES))
        return cmd, REMOTE
    cmd = "sudo -n -u auu -- /usr/bin/python3 " + shlex.quote(remote_helper()) + " snapshot"
    return cmd, ""


def remote_rev_invocation(user: str | None = None) -> tuple[str, str]:
    user = backup_user() if user is None else user
    if user == "root":
        repo = _remote_path(os.environ.get("AUU_BACKUP_REMOTE_REPO", REMOTE_REPO), "AUU_BACKUP_REMOTE_REPO")
        return "python3 - " + shlex.quote(repo), root_rev_stdin()
    return "/usr/bin/python3 " + shlex.quote(remote_helper()) + " rev", ""


def assert_remote_dir(user: str, path: str) -> None:
    if not path or any(ch in path for ch in "\n\r\x00") or any(part == ".." for part in path.split("/")):
        raise RuntimeError("unexpected remote dir")
    prefix = "/tmp/auu-bk-" if user == "root" else "/var/backups/auu-stage/auu-bk-"
    rest = path[len(prefix):] if path.startswith(prefix) else ""
    if not re.fullmatch(r"[A-Za-z0-9_-]+", rest or ""):
        raise RuntimeError("unexpected remote dir")


def remote_cleanup_command(user: str, path: str) -> str:
    assert_remote_dir(user, path)
    return "rm -rf -- " + shlex.quote(path)


def connect():
    import paramiko

    host, key, known = backup_host(), os.environ.get("AUU_BACKUP_KEY", "").strip(), os.environ.get("AUU_BACKUP_KNOWN_HOSTS", "").strip()
    if not (host and key and known):
        raise SystemExit("set AUU_BACKUP_HOST, AUU_BACKUP_KEY and AUU_BACKUP_KNOWN_HOSTS (see ops/backup/README.md)")
    port, user = backup_port(), backup_user()
    pinned_host_keys(known, host, port)
    c = paramiko.SSHClient()
    c.load_host_keys(known)  # pinned file only; never the system known_hosts, never auto-accept
    c.set_missing_host_key_policy(paramiko.RejectPolicy())
    c.connect(host, port=port, username=user, key_filename=key, timeout=20, allow_agent=False, look_for_keys=False)
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
    user = backup_user()
    c = connect()
    remote_dir = None
    try:
        snap_cmd, snap_in = remote_snapshot_invocation(user)
        rc, out, err = run(c, snap_cmd, stdin=snap_in)
        if rc != 0:
            raise RuntimeError(f"remote snapshot failed rc={rc}: {err[-400:]}")
        res = json.loads(out.strip().splitlines()[-1])
        remote_dir = res["dir"]
        assert_remote_dir(user, remote_dir)
        rev_cmd, rev_in = remote_rev_invocation(user)
        rev_rc, rev_out, _rev_err = run(c, rev_cmd, stdin=rev_in)
        rev, rev_source = parse_rev_output(rev_out if rev_rc == 0 else "")
        sftp = c.open_sftp()
        manifest = {"created_bj": datetime.now(BJ).isoformat(timespec="seconds"), "host": backup_host(),
                    "port": backup_port(), "ssh_user": user, "server_rev": rev, "server_rev_source": rev_source,
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
        if remote_dir:
            try:
                run(c, remote_cleanup_command(user, remote_dir))
            except RuntimeError:
                log("remote temp dir was not removed (unexpected path)")
        c.close()
    (work / "MANIFEST.json").write_text(json.dumps(manifest, indent=2, sort_keys=True))
    sums = [f"{m['sha256']}  {n}\n{m['gz_sha256']}  {m['gz']}" for n, m in sorted(manifest["files"].items()) if not m.get("missing")]
    (work / "SHA256SUMS").write_text("\n".join(sums) + "\n")
    final = DEST / ts
    work.rename(final)
    os.chmod(final, 0o700)
    log(f"backup {final.name} port={manifest['port']} rev={rev or 'missing'}: " + ", ".join(
        f"{n} {m.get('bytes', 0) // 1024}KB" for n, m in manifest["files"].items() if not m.get("missing")))
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
