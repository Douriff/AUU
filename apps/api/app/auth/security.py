"""Account security (P1-4): optional TOTP 2FA, one-time recovery codes, login records, sign out other sessions.

- TOTP (RFC 6238, pyotp, MIT): 30 s step, 6 digits, ±1 step drift, a step is accepted at most once (replay guard).
- The TOTP secret is stored encrypted (Fernet from ``cryptography``; key derived with HKDF from
  ``AUU_SESSION_SECRET``). It is the user's own login factor, never an exchange key.
- Recovery codes: 10 random codes shown once; only SHA-256 hashes are stored; each works once.
- Off by default for every account. Accounts without ``totp.enabled`` log in exactly as before.
- Login records (time, IP, user agent, result) live in ``auth_log.sqlite`` (data dir), 90 days / 200 per user.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional

from app.data_paths import data_dir, guarded_path

ISSUER = "AUUTRADE"
RECOVERY_N = 10
TICKET_TTL = 300
SETUP_TTL = 900
_LOG_DAYS = 90
_LOG_KEEP = 200


def _session_secret() -> bytes:
    return os.getenv("AUU_SESSION_SECRET", "auu-paper-dev").encode("utf-8")


def _fernet():
    from cryptography.fernet import Fernet
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=b"auu-totp-salt-v1", info=b"auu-totp-secret-v1").derive(_session_secret())
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt(secret: str) -> str:
    return _fernet().encrypt(secret.encode()).decode()


def decrypt(token: str) -> str:
    return _fernet().decrypt(token.encode()).decode()


def normalize_secret(secret: str) -> str:
    """Uppercase Base32 without spaces or RFC 4648 padding (iOS Google Authenticator rejects '=')."""
    return "".join(ch for ch in (secret or "").upper() if ch.isalnum()).rstrip("=")


def new_secret() -> str:
    import pyotp

    return normalize_secret(pyotp.random_base32())


def otpauth_uri(secret: str, account: str) -> str:
    """Google Authenticator Key URI (https://github.com/google/google-authenticator/wiki/Key-Uri-Format).

    Label is ``Issuer:account`` with both sides percent-encoded (``safe=''`` so ``/`` etc. cannot
    break the path). Query carries ``secret`` + ``issuer`` only — omit algorithm/digits/period so
    apps use SHA1 / 6 / 30 defaults (some clients reject non-default or lowercase algorithm).
    """
    from urllib.parse import quote, urlencode

    secret = normalize_secret(secret)
    if not secret or any(ch not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567" for ch in secret):
        raise ValueError("TOTP_SECRET_BAD")
    acct = quote((account or "user").strip() or "user", safe="")
    label = f"{quote(ISSUER, safe='')}:{acct}"
    query = urlencode({"secret": secret, "issuer": ISSUER})
    return f"otpauth://totp/{label}?{query}"


def qr_png_data_uri(uri: str) -> str:
    """PNG data-URI QR (segno). border=4 meets the quiet-zone spec — critical on our dark UI so
    Google Authenticator can find the symbol; SVG + border=2 was failing phone scans while manual
    secret entry still worked."""
    import base64
    import io

    import segno

    buf = io.BytesIO()
    segno.make(uri, error="m").save(buf, kind="png", scale=8, border=4, dark="#000000", light="#ffffff")
    return "data:image/png;base64," + base64.standard_b64encode(buf.getvalue()).decode("ascii")


def qr_svg(uri: str) -> str:
    """Legacy SVG helper (tests / fallback). Prefer ``qr_png_data_uri`` for the setup UI."""
    import io

    import segno

    buf = io.BytesIO()
    segno.make(uri, error="m").save(buf, kind="svg", scale=8, border=4, dark="#000", light="#fff", xmldecl=False, svgns=True)
    return buf.getvalue().decode()


def match_step(secret: str, code: str, *, now: Optional[float] = None, last_step: int = -1) -> Optional[int]:
    """Return the matched 30 s step for a 6-digit code (±1 step), or None. Steps <= last_step are refused (replay)."""
    import pyotp

    code = "".join(ch for ch in (code or "") if ch.isdigit())
    if len(code) != 6:
        return None
    t = time.time() if now is None else now
    totp = pyotp.TOTP(normalize_secret(secret))
    step = int(t // 30)
    for s in (step, step - 1, step + 1):
        if s > last_step and hmac.compare_digest(totp.at(s * 30), code):
            return s
    return None


# ---- recovery codes ------------------------------------------------------------------------
_ALPHA = "abcdefghjkmnpqrstuvwxyz23456789"


def _norm_recovery(code: str) -> str:
    return "".join(ch for ch in (code or "").lower() if ch.isalnum())


def recovery_hash(code: str) -> str:
    return hashlib.sha256(("auu-recovery-v1:" + _norm_recovery(code)).encode()).hexdigest()


def new_recovery_codes() -> list[str]:
    out = []
    for _ in range(RECOVERY_N):
        raw = "".join(secrets.choice(_ALPHA) for _ in range(10))
        out.append(f"{raw[:5]}-{raw[5:]}")
    return out


def looks_like_recovery(code: str) -> bool:
    return len(_norm_recovery(code)) == 10 and not _norm_recovery(code).isdigit()


# ---- login tickets (password ok, TOTP pending) ---------------------------------------------
def issue_ticket(user_id: str, epoch: int, *, now: Optional[int] = None) -> str:
    exp = str(int(time.time() if now is None else now) + TICKET_TTL)
    nonce = secrets.token_hex(8)
    msg = f"2fa.{user_id}.{exp}.{nonce}.e{epoch}"
    sig = hmac.new(_session_secret(), msg.encode(), hashlib.sha256).hexdigest()
    return f"{user_id}.{exp}.{nonce}.{sig}"


def read_ticket(ticket: str, epoch_of) -> Optional[str]:
    parts = (ticket or "").split(".")
    if len(parts) != 4:
        return None
    uid, exp, nonce, sig = parts
    try:
        if int(exp) < int(time.time()):
            return None
    except ValueError:
        return None
    msg = f"2fa.{uid}.{exp}.{nonce}.e{epoch_of(uid)}"
    want = hmac.new(_session_secret(), msg.encode(), hashlib.sha256).hexdigest()
    return uid if hmac.compare_digest(sig, want) else None


# ---- per-user state helpers (dict stored inside users.json under "totp") -------------------
def enabled(user: dict) -> bool:
    return bool((user.get("totp") or {}).get("enabled"))


def verify_second_factor(user: dict, code: str, *, now: Optional[float] = None) -> Optional[str]:
    """Check a TOTP or recovery code against an enabled user; mutates user['totp'] (step / used code).
    Returns 'totp' | 'recovery' | None. Caller saves the user store."""
    t = user.get("totp") or {}
    if not t.get("enabled"):
        return None
    if looks_like_recovery(code):
        h = recovery_hash(code)
        left = list(t.get("recovery") or [])
        for i, x in enumerate(left):
            if hmac.compare_digest(x, h):
                left.pop(i)
                t["recovery"] = left
                return "recovery"
        return None
    try:
        secret = decrypt(str(t.get("secret_enc") or ""))
    except Exception:
        return None
    step = match_step(secret, code, now=now, last_step=int(t.get("last_step") or -1))
    if step is None:
        return None
    t["last_step"] = step
    return "totp"


# ---- login records -------------------------------------------------------------------------
_SCHEMA = """
CREATE TABLE IF NOT EXISTS logins (
  id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL, ts INTEGER NOT NULL,
  ip TEXT, ua TEXT, result TEXT NOT NULL, method TEXT
);
CREATE INDEX IF NOT EXISTS logins_user ON logins(user_id, ts);
"""


class LoginLog:
    def __init__(self, path: Optional[Path | str] = None):
        self.path = guarded_path(Path(path) if path else data_dir() / "auth_log.sqlite")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None, timeout=30)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(_SCHEMA)
        self._lock = threading.Lock()

    def add(self, user_id: str, ip: str, ua: str, result: str, method: str = "") -> None:
        now = int(time.time() * 1000)
        with self._lock:
            self._db.execute("INSERT INTO logins(user_id, ts, ip, ua, result, method) VALUES (?,?,?,?,?,?)",
                             (user_id, now, (ip or "")[:64], (ua or "")[:200], result, method))
            self._db.execute("DELETE FROM logins WHERE ts<?", (now - _LOG_DAYS * 86_400_000,))
            self._db.execute("DELETE FROM logins WHERE user_id=? AND id NOT IN (SELECT id FROM logins WHERE user_id=? ORDER BY id DESC LIMIT ?)",
                             (user_id, user_id, _LOG_KEEP))

    def recent(self, user_id: str, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute("SELECT ts, ip, ua, result, method FROM logins WHERE user_id=? ORDER BY id DESC LIMIT ?",
                                    (user_id, int(limit))).fetchall()
        return [dict(r) for r in rows]

    def close(self) -> None:
        with self._lock:
            self._db.close()


_log: Optional[LoginLog] = None
_llock = threading.Lock()


def login_log() -> LoginLog:
    global _log
    with _llock:
        if _log is None:
            _log = LoginLog()
        return _log


def reset_login_log() -> None:
    global _log
    with _llock:
        if _log is not None:
            try:
                _log.close()
            except Exception:
                pass
        _log = None
