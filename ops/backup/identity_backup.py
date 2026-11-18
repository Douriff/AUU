#!/usr/bin/env python3
"""Encrypt users.json and the session secret into one file outside this repository.

The official SQLite backup deliberately excludes both. This tool does not print
the secret, user names, password hashes, or the passphrase, and it refuses to
write the bundle inside the git work tree.

Passphrase: AUU_IDENTITY_BACKUP_PASSPHRASE (at least 16 characters; never an argument).
Inputs: AUU_USER_STORE (path) and AUU_SESSION_SECRET (value already in the environment).

  python3 ops/backup/identity_backup.py backup /safe/auu-identity.bin
  python3 ops/backup/identity_backup.py verify /safe/auu-identity.bin
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
MAGIC = b"AUUID1"
KDF_ROUNDS = 200_000


def _die(msg: str, code: int = 2) -> None:
    print(msg, file=sys.stderr)
    raise SystemExit(code)


def _passphrase() -> bytes:
    raw = os.environ.get("AUU_IDENTITY_BACKUP_PASSPHRASE") or ""
    if len(raw) < 16:
        _die("AUU_IDENTITY_BACKUP_PASSPHRASE must be at least 16 characters")
    return raw.encode("utf-8")


def _inside_repo(path: Path) -> bool:
    try:
        path.resolve().relative_to(REPO.resolve())
    except ValueError:
        return False
    return True


def _aes():
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
        from cryptography.hazmat.primitives import hashes
    except ImportError:
        _die("cryptography is required (already used by the API for TOTP)")
    return AESGCM, PBKDF2HMAC, hashes


def _key(password: bytes, salt: bytes):
    _AESGCM, PBKDF2HMAC, hashes = _aes()
    return PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=KDF_ROUNDS).derive(password)


def _load() -> tuple[bytes, bytes, int]:
    store = (os.environ.get("AUU_USER_STORE") or "").strip()
    secret = os.environ.get("AUU_SESSION_SECRET") or ""
    if not store:
        _die("set AUU_USER_STORE to the users.json path")
    path = Path(store)
    if _inside_repo(path):
        _die("refusing to read a user store from inside the repository")
    if not secret or secret == "auu-paper-dev":
        _die("set AUU_SESSION_SECRET to the real server value, not the development fallback")
    try:
        blob = path.read_bytes()
        doc = json.loads(blob.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        _die(f"users.json is not readable JSON ({type(exc).__name__})")
    users = doc.get("users") if isinstance(doc, dict) else None
    if not isinstance(users, list):
        _die("users.json has no users list")
    return blob, secret.encode("utf-8"), len(users)


def _pack(users: bytes, secret: bytes, count: int) -> bytes:
    body = json.dumps({
        "v": 1,
        "user_count": count,
        "users_sha256": hashlib.sha256(users).hexdigest(),
        "secret_sha256": hashlib.sha256(secret).hexdigest(),
        "users": users.decode("utf-8"),
        "session_secret": secret.decode("utf-8"),
    }, ensure_ascii=False).encode("utf-8")
    AESGCM, _kdf, _hashes = _aes()
    salt, nonce = os.urandom(16), os.urandom(12)
    ct = AESGCM(_key(_passphrase(), salt)).encrypt(nonce, body, MAGIC)
    return MAGIC + salt + nonce + ct


def _unpack(blob: bytes) -> dict:
    if not blob.startswith(MAGIC) or len(blob) < len(MAGIC) + 16 + 12 + 16:
        _die("not an identity bundle")
    salt, nonce, ct = blob[len(MAGIC):len(MAGIC) + 16], blob[len(MAGIC) + 16:len(MAGIC) + 28], blob[len(MAGIC) + 28:]
    AESGCM, _kdf, _hashes = _aes()
    try:
        raw = AESGCM(_key(_passphrase(), salt)).decrypt(nonce, ct, MAGIC)
        doc = json.loads(raw.decode("utf-8"))
    except Exception:
        _die("decrypt failed")
    users = doc.get("users")
    secret = doc.get("session_secret")
    if not isinstance(users, str) or not isinstance(secret, str):
        _die("bundle is missing fields")
    users_b, secret_b = users.encode("utf-8"), secret.encode("utf-8")
    if hashlib.sha256(users_b).hexdigest() != doc.get("users_sha256"):
        _die("users hash mismatch")
    if hashlib.sha256(secret_b).hexdigest() != doc.get("secret_sha256"):
        _die("session secret hash mismatch")
    try:
        parsed = json.loads(users)
    except json.JSONDecodeError:
        _die("users payload is not JSON")
    rows = parsed.get("users") if isinstance(parsed, dict) else None
    if not isinstance(rows, list) or len(rows) != int(doc.get("user_count") or -1):
        _die("user count mismatch")
    if not secret:
        _die("session secret is empty")
    return {"user_count": len(rows)}


def backup(dest: Path) -> None:
    if _inside_repo(dest):
        _die("refusing to write the bundle inside the repository")
    users, secret, count = _load()
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    tmp.write_bytes(_pack(users, secret, count))
    os.chmod(tmp, 0o600)
    tmp.replace(dest)
    print(f"ok users={count} bytes={dest.stat().st_size}")


def verify(dest: Path) -> None:
    if _inside_repo(dest):
        _die("refusing to read a bundle from inside the repository")
    try:
        blob = dest.read_bytes()
    except OSError as exc:
        _die(f"cannot read bundle ({type(exc).__name__})")
    doc = _unpack(blob)
    print(f"ok users={doc['user_count']}")


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[0] not in {"backup", "verify"}:
        _die("usage: identity_backup.py backup|verify OUT")
    dest = Path(argv[1]).expanduser()
    if argv[0] == "backup":
        backup(dest)
    else:
        verify(dest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
