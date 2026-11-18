"""ops/backup/auu_backup.py: remote snapshot script, manifest verification, restore drill, retention (no network)."""
from __future__ import annotations

import gzip
import grp
import importlib.util
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
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


def _load_named(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _key(path: Path):
    import paramiko

    subprocess.run(["ssh-keygen", "-t", "ed25519", "-f", str(path), "-N", "", "-q"], check=True, capture_output=True)
    return paramiko.Ed25519Key(filename=str(path))


class BackupTransportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.env = patch.dict(os.environ)
        self.env.start()
        for name in ("AUU_BACKUP_PORT", "AUU_BACKUP_USER", "AUU_BACKUP_HOST", "AUU_BACKUP_KEY",
                     "AUU_BACKUP_KNOWN_HOSTS", "AUU_BACKUP_REMOTE_HELPER"):
            os.environ.pop(name, None)
        self.m = _load_named("auu_backup_transport", ROOT / "ops" / "backup" / "auu_backup.py")
        self.helper = _load_named("auu_remote_snapshot", ROOT / "ops" / "backup" / "auu_remote_snapshot.py")

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_port_and_root_defaults(self):
        self.assertEqual(self.m.backup_port(None), 22)
        self.assertEqual(self.m.backup_port(""), 22)
        self.assertEqual(self.m.backup_port(" 16683 "), 16683)
        self.assertEqual(self.m.backup_port("22"), 22)
        for bad in ("0", "65536", "22.0", "ssh", "-1"):
            with self.assertRaises(SystemExit):
                self.m.backup_port(bad)
        self.assertEqual(self.m.backup_user(), "root")
        self.assertEqual(self.m.hostkey_name("203.0.113.10", 22), "203.0.113.10")
        self.assertEqual(self.m.hostkey_name("203.0.113.10", 16683), "[203.0.113.10]:16683")
        cmd, stdin = self.m.remote_snapshot_invocation()
        self.assertTrue(cmd.startswith("runuser -u auu -- python3 - "))
        self.assertNotIn("sudo", cmd)
        self.assertIn("mainstream_strategy.sqlite", cmd)
        self.assertIn("PRAGMA integrity_check", stdin)
        rev_cmd, rev_in = self.m.remote_rev_invocation()
        self.assertTrue(rev_cmd.startswith("python3 - "))
        self.assertIn("read_server_rev", rev_in)
        self.assertNotIn("users.json", self.m.FILES)
        self.assertEqual(self.helper.FILES, self.m.FILES)

    def test_restricted_user_does_not_send_a_shell_program(self):
        with patch.dict(os.environ, {"AUU_BACKUP_USER": "auubackup"}):
            cmd, stdin = self.m.remote_snapshot_invocation()
            rev_cmd, rev_in = self.m.remote_rev_invocation()
        self.assertEqual(stdin, "")
        self.assertEqual(cmd, "sudo -n -u auu -- /usr/bin/python3 /usr/local/sbin/auu_remote_snapshot.py snapshot")
        self.assertEqual(rev_in, "")
        self.assertEqual(rev_cmd, "/usr/bin/python3 /usr/local/sbin/auu_remote_snapshot.py rev")
        self.assertNotIn("sudo", rev_cmd)
        self.assertEqual(self.helper.main(["rev", "/etc/auu"]), 2)
        self.assertEqual(self.helper.main(["snapshot", "/etc/auu/auu.env"]), 2)

    def test_cleanup_rejects_paths_outside_the_account_prefix(self):
        self.assertEqual(self.m.remote_cleanup_command("root", "/tmp/auu-bk-abc"), "rm -rf -- /tmp/auu-bk-abc")
        stage = self.m.remote_cleanup_command("auubackup", "/var/backups/auu-stage/auu-bk-abc")
        self.assertEqual(stage, "rm -rf -- /var/backups/auu-stage/auu-bk-abc")
        for user, path in (
            ("root", "/var/backups/auu-stage/auu-bk-abc"),
            ("auubackup", "/tmp/auu-bk-abc"),
            ("root", "/tmp/auu-bk-abc;rm"),
            ("root", "/tmp/auu-bk-../etc"),
            ("auubackup", "/var/backups/auu-stage/auu-bk-abc/extra"),
        ):
            with self.assertRaises(RuntimeError):
                self.m.remote_cleanup_command(user, path)

    def test_known_hosts_port_does_not_reuse_the_bare_name(self):
        key = _key(self.root / "host")
        bare = self.root / "bare"
        bare.write_text(f"backup.example {key.get_name()} {key.get_base64()}\n")
        self.assertEqual(self.m.pinned_host_keys(str(bare), "backup.example", 22), "backup.example")
        with self.assertRaises(SystemExit) as raised:
            self.m.pinned_host_keys(str(bare), "backup.example", 16683)
        self.assertIn("[backup.example]:16683", str(raised.exception))
        bracket = self.root / "bracket"
        bracket.write_text(f"[backup.example]:16683 {key.get_name()} {key.get_base64()}\n")
        self.assertEqual(self.m.pinned_host_keys(str(bracket), "backup.example", 16683), "[backup.example]:16683")
        with self.assertRaises(SystemExit):
            self.m.pinned_host_keys(str(bracket), "backup.example", 22)

    def test_commit_file_is_the_rev_when_git_is_absent(self):
        repo = self.root / "release"
        repo.mkdir()
        full = "c111ed95f8816956e24c8a64f932b1dd0f6a2316"
        (repo / "COMMIT").write_text(full + "\nbuilt locally\n")
        self.assertEqual(self.helper.read_server_rev(str(repo)), ("commit", full))
        link = self.root / "opt-auu"
        link.symlink_to(repo, target_is_directory=True)
        self.assertEqual(self.helper.read_server_rev(str(link)), ("commit", full))
        (repo / "COMMIT").write_text("production\n")
        self.assertEqual(self.helper.read_server_rev(str(repo)), ("missing", ""))
        (repo / "COMMIT").unlink()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        (repo / "README").write_text("x\n")
        subprocess.run(["git", "add", "README"], cwd=repo, check=True)
        subprocess.run(["git", "-c", "user.email=backup@example.com", "-c", "user.name=backup",
                        "commit", "-q", "-m", "init"], cwd=repo, check=True)
        short = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], cwd=repo, text=True).strip()
        (repo / "COMMIT").write_text("aaaaaaaaaaa\n")
        self.assertEqual(self.helper.read_server_rev(str(repo)), ("git", short))
        script = self.m.root_rev_stdin()
        proc = subprocess.run([sys.executable, "-", str(repo)], input=script, text=True, capture_output=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.m.parse_rev_output(proc.stdout), (short, "git"))
        shutil.rmtree(repo / ".git")
        (repo / "COMMIT").write_text(full + "\n")
        proc = subprocess.run([sys.executable, "-", str(repo)], input=script, text=True, capture_output=True, timeout=30)
        self.assertEqual(self.m.parse_rev_output(proc.stdout), (full, "commit"))
        self.assertEqual(self.m.parse_rev_output("fatal: not a git repository"), ("", "missing"))
        self.assertEqual(self.m.parse_rev_output("commit production"), ("", "missing"))

    def test_snapshot_helper_leaves_users_and_env_behind(self):
        data = self.root / "data"
        data.mkdir()
        con = sqlite3.connect(data / "mainstream_strategy.sqlite")
        con.execute("CREATE TABLE runs (id INTEGER)")
        con.execute("INSERT INTO runs VALUES (1)")
        con.commit()
        con.close()
        (data / "users.json").write_text('{"users":[{"name":"secret-user"}]}')
        (data / "auu.env").write_text("AUU_SESSION_SECRET=not-for-backup\n")
        stage = self.root / "stage"
        stage.mkdir()
        os.chown(stage, os.getuid(), os.getgid())
        os.chmod(stage, 0o770)
        group = grp.getgrgid(os.getgid()).gr_name
        with self.assertRaises(SystemExit):
            self.helper.assert_stage(str(stage))
        os.chmod(stage, 0o777)
        with self.assertRaises(SystemExit):
            self.helper.assert_stage(str(stage), group)
        os.chmod(stage, 0o770)
        res = self.helper.snapshot_dir(str(data), self.helper.FILES, str(stage), group)
        try:
            names = set(os.listdir(res["dir"]))
            self.assertNotIn("users.json", names)
            self.assertNotIn("auu.env", names)
            self.assertNotIn("users.json", res["files"])
            self.assertEqual(res["files"]["mainstream_strategy.sqlite"]["counts"]["runs"], 1)
            self.assertEqual(os.stat(os.path.join(res["dir"], "mainstream_strategy.sqlite")).st_mode & 0o777, 0o640)
            blob = b"".join(Path(res["dir"], name).read_bytes() for name in names)
            self.assertNotIn(b"secret-user", blob)
            self.assertNotIn(b"not-for-backup", blob)
        finally:
            shutil.rmtree(res["dir"], ignore_errors=True)

    def test_connect_requires_the_nonstandard_port_entry(self):
        import paramiko

        host_key = _key(self.root / "srv")
        user_key = _key(self.root / "client")
        sock = socket.socket()
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        sock.listen(4)
        port = sock.getsockname()[1]
        stop = threading.Event()
        errors = []

        def handle(conn):
            transport = paramiko.Transport(conn)
            transport.add_server_key(host_key)
            try:
                transport.start_server(server=Allow(user_key.asbytes()))
                time.sleep(0.3)
            except Exception as exc:
                errors.append(exc)
            finally:
                transport.close()

        def serve():
            sock.settimeout(0.2)
            while not stop.is_set():
                try:
                    conn, _addr = sock.accept()
                except socket.timeout:
                    continue
                threading.Thread(target=handle, args=(conn,), daemon=True).start()

        class Allow(paramiko.ServerInterface):
            def __init__(self, blob: bytes):
                self.blob = blob

            def get_allowed_auths(self, username):
                return "publickey"

            def check_auth_publickey(self, username, key):
                if key.asbytes() == self.blob:
                    return paramiko.AUTH_SUCCESSFUL
                return paramiko.AUTH_FAILED

            def check_channel_request(self, kind, chanid):
                return paramiko.OPEN_SUCCEEDED

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        self.addCleanup(stop.set)
        self.addCleanup(sock.close)
        good = self.root / "good_hosts"
        good.write_text(f"[127.0.0.1]:{port} {host_key.get_name()} {host_key.get_base64()}\n")
        bare = self.root / "bare_hosts"
        bare.write_text(f"127.0.0.1 {host_key.get_name()} {host_key.get_base64()}\n")
        wrong = self.root / "wrong_hosts"
        wrong.write_text(f"[127.0.0.1]:{port} {user_key.get_name()} {user_key.get_base64()}\n")
        with patch.dict(os.environ, {"AUU_BACKUP_HOST": "127.0.0.1", "AUU_BACKUP_PORT": str(port),
                                     "AUU_BACKUP_USER": "root", "AUU_BACKUP_KEY": str(self.root / "client"),
                                     "AUU_BACKUP_KNOWN_HOSTS": str(bare)}):
            with self.assertRaises(SystemExit):
                self.m.connect()
        with patch.dict(os.environ, {"AUU_BACKUP_HOST": "127.0.0.1", "AUU_BACKUP_PORT": str(port),
                                     "AUU_BACKUP_USER": "auubackup", "AUU_BACKUP_KEY": str(self.root / "client"),
                                     "AUU_BACKUP_KNOWN_HOSTS": str(wrong)}):
            with self.assertRaises(paramiko.BadHostKeyException):
                self.m.connect()
        with patch.dict(os.environ, {"AUU_BACKUP_HOST": "127.0.0.1", "AUU_BACKUP_PORT": str(port),
                                     "AUU_BACKUP_USER": "auubackup", "AUU_BACKUP_KEY": str(self.root / "client"),
                                     "AUU_BACKUP_KNOWN_HOSTS": str(good)}):
            client = self.m.connect()
            client.close()
        stop.set()
        thread.join(timeout=3)
        self.assertFalse(thread.is_alive())


if __name__ == "__main__":
    unittest.main()
