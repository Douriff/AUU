"""Local paper accounts. Passwords are bcrypt hashes. No custody of funds."""
from __future__ import annotations

import fcntl
import hmac
import json
import os
import re
import secrets
import threading
import time
import uuid
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
from typing import Any, Iterator, Optional

import bcrypt

from app.auth.email_codes import check_code, email_ok, email_verify_enabled, normalize_email, reset_email_codes
from app.data_paths import data_dir, guarded_path
from app.paper.books import user_day_pnl
from app.paper.ledger import PaperTradeJournal, _load_journal

_NAME = re.compile(r"^[A-Za-z0-9_\u4e00-\u9fff][A-Za-z0-9_.@\-\u4e00-\u9fff]{1,31}$")
_DISPLAY = re.compile(r"^[A-Za-z0-9_\u4e00-\u9fff][A-Za-z0-9_ \u4e00-\u9fff]{0,23}$")
PASSWORD_MIN = 8
PASSWORD_MAX_BYTES = 72
_LOCK = threading.RLock()
_USERS: Optional[list[dict[str, Any]]] = None
_STORE_META: Optional[tuple[int, int, int]] = None
_STORE_CORRUPT = False
_TXN = threading.local()
_BOOKS: dict[str, PaperTradeJournal] = {}
_ATTEMPTS: dict[str, list[float]] = {}
_HIDDEN = {"password", "password_hash", "password_confirm", "current_password", "new_password", "new_password_confirm", "invite", "email_code", "session_epoch", "sessions", "totp", "totp_code"}
# One cookie is one device. Logout revokes that device only; other devices stay until
# revoke-others, a password change, or this cap drops the oldest.
MAX_DEVICES = 8


def reset_accounts() -> None:
    global _USERS, _STORE_META, _STORE_CORRUPT
    with _LOCK:
        _USERS = None
        _STORE_META = None
        _STORE_CORRUPT = False
        _BOOKS.clear()
        _ATTEMPTS.clear()
        _FAILURES.clear()
    reset_email_codes()
    from app.auth.security import reset_login_log

    reset_login_log()


def auth_enabled() -> bool:
    return os.getenv("AUU_AUTH", "off").strip().lower() in {"1", "true", "on", "yes"}


def signup_allowed() -> bool:
    raw = os.getenv("AUU_ALLOW_SIGNUP", "on" if auth_enabled() else "off")
    return raw.strip().lower() in {"1", "true", "on", "yes"}


def invite_code() -> str:
    return os.getenv("AUU_INVITE_CODE", "").strip()


def start_sol_default() -> float:
    try:
        return max(1.0, float(os.getenv("AUU_PAPER_START_SOL", "100")))
    except ValueError:
        return 100.0


def _store_path() -> Path:
    raw = (os.getenv("AUU_USER_STORE") or "").strip()
    path = Path(raw).expanduser() if raw else data_dir() / "users.json"
    return guarded_path(path)


def _journal_dir() -> Path:
    raw = (os.getenv("AUU_USER_JOURNAL_DIR") or "").strip()
    path = Path(raw).expanduser() if raw else _store_path().parent / "user_journals"
    return guarded_path(path)


def _session_secret() -> bytes:
    return os.getenv("AUU_SESSION_SECRET", "auu-paper-dev").encode("utf-8")


class StoreUnavailable(ValueError):
    """The user store is unreadable or internally inconsistent. Fail closed; do not rewrite it."""

    def __init__(self, code: str = "STORE_UNAVAILABLE"):
        super().__init__(code)


def _lock_path() -> Path:
    path = _store_path()
    return path.with_name(path.name + ".lock")


def _stat_tuple(path: Path) -> Optional[tuple[int, int, int]]:
    try:
        st = path.stat()
        return (st.st_mtime_ns, st.st_size, st.st_ino)
    except OSError:
        return None


@contextmanager
def _store_lock() -> Iterator[None]:
    """Exclusive inter-process lock, then the in-process RLock.

    Unique order is always flock then RLock. If this thread already holds the
    transaction, yield without opening another lock fd (POSIX fcntl locks are
    per-process; a nested fd close could drop the outer lock).

    Supported topology is one or more local processes sharing a filesystem that
    implements POSIX fcntl advisory locks (the deployed API host uses a local
    data directory). This is not a claim about NFS or a remote database.
    """
    if getattr(_TXN, "held", False):
        yield
        return
    path = _lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "a+b")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX)
        with _LOCK:
            _TXN.held = True
            try:
                yield
            finally:
                _TXN.held = False
    finally:
        try:
            fcntl.flock(fh, fcntl.LOCK_UN)
        finally:
            fh.close()


@contextmanager
def _store_txn() -> Iterator[list[dict[str, Any]]]:
    """Lock, reload the on-disk users list, and let the caller mutate that list."""
    with _store_lock():
        yield _reload_locked()


def _user_row(users: list[dict[str, Any]], user_id: str) -> Optional[dict[str, Any]]:
    for row in users:
        if row.get("id") == user_id:
            return row
    return None


# Known optional fields kept as-is when present and well-typed. Unknown extra
# keys are preserved for historical stores. Required: non-empty string id + name.
_OPTIONAL_TYPES: dict[str, tuple[type, ...]] = {
    "display_name": (str,),
    "password_hash": (str,),
    "email": (str,),
    "start_sol": (int, float),
    "is_admin": (bool,),
    "created_ts": (int, float),
    "session_epoch": (int, float),
    "sessions": (list,),
    "totp": (dict,),
    "locale": (str,),
    "locale_ts": (int, float),
}


def _decode_users(raw: Any) -> list[dict[str, Any]]:
    """Validate the whole users array. Invalid or duplicate rows fail closed; nothing is filtered out."""
    if not isinstance(raw, dict) or "users" not in raw:
        raise StoreUnavailable("STORE_UNAVAILABLE")
    rows = raw.get("users")
    if not isinstance(rows, list):
        raise StoreUnavailable("STORE_UNAVAILABLE")
    seen_ids: set[str] = set()
    seen_names: set[str] = set()
    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            raise StoreUnavailable("STORE_UNAVAILABLE")
        uid = row.get("id")
        name = row.get("name")
        if not isinstance(uid, str) or not uid.strip() or not isinstance(name, str) or not name.strip():
            raise StoreUnavailable("STORE_UNAVAILABLE")
        if uid in seen_ids or name.lower() in seen_names:
            raise StoreUnavailable("STORE_UNAVAILABLE")
        seen_ids.add(uid)
        seen_names.add(name.lower())
        for field, types in _OPTIONAL_TYPES.items():
            if field not in row or row[field] is None:
                continue
            if not isinstance(row[field], types):
                raise StoreUnavailable("STORE_UNAVAILABLE")
        sessions = row.get("sessions")
        if isinstance(sessions, list):
            if any(not isinstance(item, dict) for item in sessions):
                raise StoreUnavailable("STORE_UNAVAILABLE")
        out.append(row)
    return out


def _read_store_from_disk() -> list[dict[str, Any]]:
    """Return users from disk. Missing file is empty. Corrupt or unreadable file raises and does not rewrite."""
    global _STORE_CORRUPT
    if _STORE_CORRUPT:
        raise StoreUnavailable("STORE_UNAVAILABLE")
    path = _store_path()
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except OSError as exc:
        _STORE_CORRUPT = True
        raise StoreUnavailable("STORE_UNAVAILABLE") from exc
    try:
        return _decode_users(json.loads(text))
    except (json.JSONDecodeError, StoreUnavailable, TypeError, ValueError) as exc:
        _STORE_CORRUPT = True
        raise StoreUnavailable("STORE_UNAVAILABLE") from exc


def _reload_locked() -> list[dict[str, Any]]:
    """Reload from disk. Caller must hold _store_lock()."""
    global _USERS, _STORE_META
    users = _read_store_from_disk()
    _USERS = users
    _STORE_META = _stat_tuple(_store_path())
    return users


def _load() -> list[dict[str, Any]]:
    global _USERS
    if _STORE_CORRUPT:
        raise StoreUnavailable("STORE_UNAVAILABLE")
    path = _store_path()
    meta = _stat_tuple(path)
    with _LOCK:
        if _USERS is not None and meta == _STORE_META:
            return _USERS
    with _store_lock():
        return _reload_locked()


def _save_locked(users: list[dict[str, Any]]) -> None:
    """Atomic write. Caller must hold _store_lock(). Refuses to overwrite a corrupt store."""
    global _USERS, _STORE_META
    if _STORE_CORRUPT:
        raise StoreUnavailable("STORE_UNAVAILABLE")
    path = guarded_path(_store_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps({"users": users}), encoding="utf-8")
    tmp.replace(path)
    _USERS = users
    _STORE_META = _stat_tuple(path)


def _save() -> None:
    """Persist the list already loaded and mutated in an open store transaction.

    Must not reload (that would drop the caller's changes) and must not dump a
    stale cache from outside a transaction (that would wipe newer rows).
    """
    if not getattr(_TXN, "held", False):
        raise RuntimeError("user store write requires an open store transaction")
    if _USERS is None:
        raise RuntimeError("user store transaction has no loaded users")
    _save_locked(_USERS)


def _has_admin(users: list[dict[str, Any]]) -> bool:
    return any(bool(row.get("is_admin")) for row in users)


def reserved_admin_name() -> str:
    return os.getenv("AUU_ADMIN_USER", "").strip()


def _is_reserved_name(name: str) -> bool:
    reserved = reserved_admin_name()
    return bool(reserved) and name.lower() == reserved.lower()


def public_signup_open() -> bool:
    """True only when env allows signup and a valid store already has an admin."""
    if not auth_enabled() or not signup_allowed():
        return False
    try:
        return _has_admin(_load())
    except StoreUnavailable:
        return False


def _rate_max() -> int:
    try:
        return max(1, int(os.getenv("AUU_AUTH_RATE_MAX", "20")))
    except ValueError:
        return 20


def _rate_window() -> float:
    try:
        return max(1.0, float(os.getenv("AUU_AUTH_RATE_WINDOW", "600")))
    except ValueError:
        return 600.0


def allow_attempt(bucket: str) -> bool:
    """Sliding window for signup and login. The bucket must not contain a password."""
    now = time.time()
    window = _rate_window()
    rows = [stamp for stamp in _ATTEMPTS.get(bucket, []) if now - stamp < window]
    if len(rows) >= _rate_max():
        _ATTEMPTS[bucket] = rows
        return False
    rows.append(now)
    _ATTEMPTS[bucket] = rows
    return True


_FAILURES: dict[str, list[float]] = {}


def _fail_max() -> int:
    try:
        return max(1, int(os.getenv("AUU_AUTH_ACCOUNT_FAIL_MAX", "10")))
    except ValueError:
        return 10


def _fail_window() -> float:
    try:
        return max(60.0, float(os.getenv("AUU_AUTH_ACCOUNT_FAIL_WINDOW", "900")))
    except ValueError:
        return 900.0


def _fail_key(name: str) -> str:
    return (name or "").strip().lower()


def _recent_failures(name: str) -> list[float]:
    now = time.time()
    rows = [t for t in _FAILURES.get(_fail_key(name), []) if now - t < _fail_window()]
    _FAILURES[_fail_key(name)] = rows
    return rows


def account_locked(name: str) -> bool:
    """Per-account lockout across all source IPs (per-IP limits live in allow_attempt/nginx)."""
    return len(_recent_failures(name)) >= _fail_max()


def note_login_failure(name: str) -> None:
    _recent_failures(name).append(time.time())


def clear_login_failures(name: str) -> None:
    _FAILURES.pop(_fail_key(name), None)


def _public(user: dict[str, Any]) -> dict[str, Any]:
    name = str(user.get("name") or "")
    shown = str(user.get("display_name") or "").strip() or name
    return {
        "id": user["id"],
        "name": name,
        "display_name": shown,
        "is_admin": bool(user.get("is_admin")),
        "start_sol": float(user.get("start_sol") or start_sol_default()),
        "created_ts": int(user.get("created_ts") or 0),
        "totp_enabled": bool((user.get("totp") or {}).get("enabled")),
    }


def scrub_secrets(payload: Any) -> Any:
    """Drop password material before a response leaves the process."""
    if isinstance(payload, dict):
        return {key: scrub_secrets(value) for key, value in payload.items() if key not in _HIDDEN}
    if isinstance(payload, list):
        return [scrub_secrets(item) for item in payload]
    return payload


def password_problem(password: str) -> Optional[str]:
    """Return an error code for a new password, or None. Special characters are allowed."""
    raw = password if isinstance(password, str) else ""
    if len(raw) < PASSWORD_MIN or len(raw.encode("utf-8")) > PASSWORD_MAX_BYTES:
        return "BAD_PASSWORD"
    if not any(ch.isalpha() for ch in raw) or not any("0" <= ch <= "9" for ch in raw):
        return "BAD_PASSWORD"
    return None


def _password_ok(password: str) -> bool:
    return password_problem(password) is None


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def passwords_match(left: str, right: str) -> bool:
    a = (left or "").encode("utf-8")
    b = (right or "").encode("utf-8")
    if len(a) != len(b):
        return False
    return hmac.compare_digest(a, b)


def verify_password(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), hashed.encode("utf-8"))
    except ValueError:
        return False


def register(
    name: str,
    password: str,
    invite: str = "",
    start_sol: Optional[float] = None,
    display_name: str = "",
    email: str = "",
    email_code: str = "",
) -> dict[str, Any]:
    if not auth_enabled():
        raise ValueError("AUTH_OFF")
    if not signup_allowed():
        raise ValueError("SIGNUP_CLOSED")
    expected = invite_code()
    if expected and not hmac.compare_digest(invite or "", expected):
        raise ValueError("INVITE")
    text = (name or "").strip()
    if not _NAME.match(text):
        raise ValueError("BAD_NAME")
    if not _password_ok(password):
        raise ValueError("BAD_PASSWORD")
    shown = (display_name or "").strip()
    if shown and not _DISPLAY.match(shown):
        raise ValueError("BAD_DISPLAY")
    start = start_sol_default() if start_sol is None else float(start_sol)
    if not (1.0 <= start <= 100_000.0):
        raise ValueError("BAD_START")
    address = ""
    if email_verify_enabled():
        address = normalize_email(email)
        if not address:
            raise ValueError("EMAIL_REQUIRED")
        if not email_ok(address):
            raise ValueError("BAD_EMAIL")
        # Consume/validate the code outside the store lock; uniqueness is rechecked under the lock.
        if user_by_email(address) is not None:
            raise ValueError("EMAIL_TAKEN")
        check_code("signup", address, email_code)
    hashed = hash_password(password)
    return _commit_new_user(
        name=text,
        password_hash=hashed,
        display_name=shown,
        email=address,
        start_sol=start,
        is_admin=False,
        require_admin_initialized=True,
        block_reserved_name=True,
    )


def _commit_new_user(
    *,
    name: str,
    password_hash: str,
    display_name: str,
    email: str,
    start_sol: float,
    is_admin: bool,
    require_admin_initialized: bool,
    block_reserved_name: bool,
) -> dict[str, Any]:
    """Reload, re-check uniqueness (name/email), assign role, and persist under one file lock."""
    user = {
        "id": uuid.uuid4().hex,
        "name": name,
        "display_name": display_name,
        "password_hash": password_hash,
        "email": email,
        "start_sol": start_sol,
        "is_admin": bool(is_admin),
        "created_ts": int(time.time() * 1000),
    }
    with _store_lock():
        users = _reload_locked()
        if require_admin_initialized and not _has_admin(users):
            raise ValueError("SIGNUP_NOT_READY")
        if block_reserved_name and _is_reserved_name(name):
            raise ValueError("NAME_TAKEN")
        if any(str(row.get("name") or "").lower() == name.lower() for row in users):
            raise ValueError("NAME_TAKEN")
        if email and any(normalize_email(str(row.get("email") or "")) == email for row in users):
            raise ValueError("EMAIL_TAKEN")
        users.append(user)
        _save_locked(users)
    return _public(user)


def bootstrap_admin(
    name: str,
    password: str,
    display_name: str = "",
    start_sol: Optional[float] = None,
) -> dict[str, Any]:
    """Controlled first-admin init. Not an HTTP route. Never overwrites a corrupt store or existing admins."""
    if not auth_enabled():
        raise ValueError("AUTH_OFF")
    text = (name or "").strip()
    if not _NAME.match(text):
        raise ValueError("BAD_NAME")
    if not _password_ok(password):
        raise ValueError("BAD_PASSWORD")
    shown = (display_name or "").strip()
    if shown and not _DISPLAY.match(shown):
        raise ValueError("BAD_DISPLAY")
    start = start_sol_default() if start_sol is None else float(start_sol)
    if not (1.0 <= start <= 100_000.0):
        raise ValueError("BAD_START")
    hashed = hash_password(password)
    with _store_lock():
        users = _reload_locked()
        if _has_admin(users):
            raise ValueError("ADMIN_EXISTS")
        if any(str(row.get("name") or "").lower() == text.lower() for row in users):
            raise ValueError("NAME_TAKEN")
        user = {
            "id": uuid.uuid4().hex,
            "name": text,
            "display_name": shown,
            "password_hash": hashed,
            "email": "",
            "start_sol": start,
            "is_admin": True,
            "created_ts": int(time.time() * 1000),
        }
        users.append(user)
        _save_locked(users)
    return _public(user)


def authenticate(name: str, password: str) -> Optional[dict[str, Any]]:
    text = (name or "").strip().lower()
    for user in _load():
        if str(user.get("name") or "").lower() == text and verify_password(password, str(user.get("password_hash") or "")):
            return user
    return None


def user_by_email(email: str) -> Optional[dict[str, Any]]:
    address = normalize_email(email)
    if not address:
        return None
    for user in _load():
        if normalize_email(str(user.get("email") or "")) == address:
            return user
    return None


def email_taken(email: str) -> bool:
    return user_by_email(email) is not None


def reset_password(email: str, code: str, new: str) -> dict[str, Any]:
    """Set a new password after an emailed code. Bumps session_epoch so old cookies stop working."""
    if not email_verify_enabled():
        raise ValueError("EMAIL_OFF")
    address = normalize_email(email)
    if not email_ok(address):
        raise ValueError("BAD_EMAIL")
    if not _password_ok(new):
        raise ValueError("BAD_PASSWORD")
    check_code("reset", address, code)
    hashed = hash_password(new)
    with _store_txn() as users:
        user = next((row for row in users if normalize_email(str(row.get("email") or "")) == address), None)
        if user is None:
            raise ValueError("EMAIL_CODE_EXPIRED")
        user["password_hash"] = hashed
        user["session_epoch"] = int(user.get("session_epoch") or 0) + 1
        user["sessions"] = []
        _save_locked(users)
    return _public(user)


def change_password(user_id: str, current: str, new: str, totp_code: str = "") -> None:
    """Current password (+ a TOTP / recovery code when 2FA is on). Bumps session_epoch: other sessions end."""
    from app.auth import security

    if not _password_ok(new):
        raise ValueError("BAD_PASSWORD")
    hashed = hash_password(new)
    with _store_txn() as users:
        user = _user_row(users, user_id)
        if user is None or not verify_password(current or "", str(user.get("password_hash") or "")):
            raise ValueError("BAD_LOGIN")
        if security.enabled(user):
            if not (totp_code or "").strip():
                raise ValueError("TOTP_REQUIRED")
            if security.verify_second_factor(user, totp_code) is None:
                _save_locked(users)
                raise ValueError("TOTP_BAD")
        user["password_hash"] = hashed
        user["session_epoch"] = int(user.get("session_epoch") or 0) + 1
        user["sessions"] = []
        _save_locked(users)


def find_user(name: str) -> Optional[dict[str, Any]]:
    text = (name or "").strip().lower()
    for user in _load():
        if str(user.get("name") or "").lower() == text:
            return user
    return None


def bump_epoch(user_id: str) -> None:
    """Invalidate every session cookie of this user (the caller re-stamps its own)."""
    with _store_txn() as users:
        user = _user_row(users, user_id)
        if user is None:
            raise ValueError("BAD_LOGIN")
        user["session_epoch"] = int(user.get("session_epoch") or 0) + 1
        user["sessions"] = []
        _save_locked(users)


def _sessions(user: Optional[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for row in (user or {}).get("sessions") or []:
        if isinstance(row, dict) and isinstance(row.get("id"), str) and row["id"]:
            try:
                issued = int(row.get("issued_ts") or 0)
            except (TypeError, ValueError):
                issued = 0
            rows.append({"id": row["id"], "issued_ts": issued})
    return rows


def session_count(user_id: str) -> int:
    return len(_sessions(user_by_id(user_id)))


def revoke_session(user_id: str, sid: str) -> bool:
    """Drop one device. Other devices of this user keep their cookies."""
    if not sid:
        return False
    with _store_txn() as users:
        user = _user_row(users, user_id)
        if user is None:
            return False
        before = _sessions(user)
        user["sessions"] = [row for row in before if row["id"] != sid]
        _save_locked(users)
        return len(user["sessions"]) != len(before)


# Interface languages the web app ships (apps/web/src/i18n/locales/*.json).
LOCALES = ("zh-CN", "en", "de", "fr", "es", "pt", "tr", "ru", "ja", "ko", "ar")


def set_locale(user_id: str, locale: str) -> dict[str, Any]:
    """Remember the user's interface language (display preference only)."""
    if locale not in LOCALES:
        raise ValueError("BAD_LOCALE")
    with _store_txn() as users:
        user = _user_row(users, user_id)
        if user is None:
            raise ValueError("BAD_LOGIN")
        user["locale"] = locale
        user["locale_ts"] = int(time.time() * 1000)
        _save_locked(users)
        view = locale_view(user)
    return view


def locale_view(user: dict[str, Any]) -> dict[str, Any]:
    loc = user.get("locale")
    return {"locale": loc if loc in LOCALES else None, "locale_ts": int(user.get("locale_ts") or 0)}


def user_locale(user: Optional[dict[str, Any]]) -> Optional[str]:
    loc = (user or {}).get("locale")
    return loc if loc in LOCALES else None


def session_epoch(user_id: str) -> int:
    return _epoch_of(user_id)


def _reverify(user: dict[str, Any], password: str, code: str) -> None:
    """Password, plus a TOTP / recovery code when 2FA is already on."""
    from app.auth import security

    if not verify_password(password or "", str(user.get("password_hash") or "")):
        raise ValueError("BAD_LOGIN")
    if security.enabled(user):
        if not (code or "").strip():
            raise ValueError("TOTP_REQUIRED")
        if security.verify_second_factor(user, code) is None:
            raise ValueError("TOTP_BAD")


def admin_step_up(user_id: str, password: str, code: str) -> None:
    """Re-confirm an admin for points changes: 2FA must already be on, then password + a fresh
    TOTP / recovery code (a TOTP step or recovery code is consumed, so it cannot be replayed)."""
    from app.auth import security

    with _store_txn() as users:
        user = _user_row(users, user_id)
        if user is None or not user.get("is_admin"):
            raise ValueError("FORBIDDEN")
        if not security.enabled(user):
            raise ValueError("POINTS_2FA_REQUIRED")
        if not (code or "").strip():
            raise ValueError("TOTP_REQUIRED")
        try:
            _reverify(user, password, code)
        except ValueError as exc:
            if str(exc) == "TOTP_BAD":
                _save_locked(users)
            raise
        _save_locked(users)


def totp_setup(user_id: str, password: str) -> dict[str, Any]:
    """Start enabling 2FA: new secret kept as pending (encrypted) until one code is verified."""
    from app.auth import security

    secret = ""
    with _store_txn() as users:
        user = _user_row(users, user_id)
        if user is None:
            raise ValueError("BAD_LOGIN")
        if security.enabled(user):
            raise ValueError("TOTP_ALREADY_ON")
        if not verify_password(password or "", str(user.get("password_hash") or "")):
            raise ValueError("BAD_LOGIN")
        secret = security.new_secret()
        t = dict(user.get("totp") or {})
        t.update(enabled=False, pending_enc=security.encrypt(secret), pending_at=int(time.time()))
        user["totp"] = t
        _save_locked(users)
        uname = str(user.get("name") or "user")
    uri = security.otpauth_uri(secret, uname)
    return {"secret": secret, "otpauth": uri, "qr_png": security.qr_png_data_uri(uri), "expires_in": security.SETUP_TTL}


def totp_enable(user_id: str, code: str) -> list[str]:
    """Verify one code against the pending secret, switch 2FA on, return the recovery codes (shown once)."""
    from app.auth import security

    with _store_txn() as users:
        user = _user_row(users, user_id)
        if user is None:
            raise ValueError("BAD_LOGIN")
        t = dict(user.get("totp") or {})
        if t.get("enabled"):
            raise ValueError("TOTP_ALREADY_ON")
        if not t.get("pending_enc") or int(time.time()) - int(t.get("pending_at") or 0) > security.SETUP_TTL:
            raise ValueError("TOTP_SETUP_EXPIRED")
        try:
            secret = security.decrypt(str(t["pending_enc"]))
        except Exception as exc:
            raise ValueError("TOTP_SETUP_EXPIRED") from exc
        step = security.match_step(secret, code)
        if step is None:
            raise ValueError("TOTP_BAD")
        codes = security.new_recovery_codes()
        user["totp"] = {"enabled": True, "secret_enc": t["pending_enc"], "last_step": step, "enabled_at": int(time.time() * 1000),
                        "recovery": [security.recovery_hash(c) for c in codes]}
        _save_locked(users)
    return codes


def totp_disable(user_id: str, password: str, code: str) -> None:
    from app.auth import security

    with _store_txn() as users:
        user = _user_row(users, user_id)
        if user is None:
            raise ValueError("BAD_LOGIN")
        if not security.enabled(user):
            raise ValueError("TOTP_OFF")
        try:
            _reverify(user, password, code)
        except ValueError as exc:
            if str(exc) == "TOTP_BAD":
                _save_locked(users)
            raise
        user.pop("totp", None)
        _save_locked(users)


def totp_new_recovery(user_id: str, password: str, code: str) -> list[str]:
    from app.auth import security

    with _store_txn() as users:
        user = _user_row(users, user_id)
        if user is None:
            raise ValueError("BAD_LOGIN")
        if not security.enabled(user):
            raise ValueError("TOTP_OFF")
        try:
            _reverify(user, password, code)
        except ValueError as exc:
            if str(exc) == "TOTP_BAD":
                _save_locked(users)
            raise
        codes = security.new_recovery_codes()
        user["totp"]["recovery"] = [security.recovery_hash(c) for c in codes]
        _save_locked(users)
    return codes


def totp_login(user_id: str, code: str) -> Optional[str]:
    """Second login step. Returns 'totp' | 'recovery' | None and persists the replay step / used code."""
    from app.auth import security

    with _store_txn() as users:
        user = _user_row(users, user_id)
        if user is None:
            return None
        how = security.verify_second_factor(user, code)
        if how is not None:
            _save_locked(users)
        return how


def security_view(user: dict[str, Any]) -> dict[str, Any]:
    t = user.get("totp") or {}
    return {"totp_enabled": bool(t.get("enabled")), "recovery_left": len(t.get("recovery") or []) if t.get("enabled") else 0,
            "totp_enabled_at": t.get("enabled_at") if t.get("enabled") else None}


def user_by_id(user_id: str) -> Optional[dict[str, Any]]:
    for user in _load():
        if user.get("id") == user_id:
            return user
    return None


def _token_sig(user_id: str, exp: str, sid: str, epoch: int) -> str:
    message = f"{user_id}.{exp}.{sid}.e{epoch}"
    return hmac.new(_session_secret(), message.encode("utf-8"), sha256).hexdigest()


def _epoch_of(user_id: str) -> int:
    user = user_by_id(user_id)
    try:
        return int((user or {}).get("session_epoch") or 0)
    except (TypeError, ValueError):
        return 0


def issue_token(user_id: str) -> str:
    """Issue one device cookie. Tokens without a device id are not accepted."""
    sid = secrets.token_hex(16)
    with _store_txn() as users:
        user = _user_row(users, user_id)
        if user is None:
            raise ValueError("BAD_LOGIN")
        rows = _sessions(user)
        rows.append({"id": sid, "issued_ts": int(time.time() * 1000)})
        if len(rows) > MAX_DEVICES:
            rows = rows[-MAX_DEVICES:]
        user["sessions"] = rows
        _save_locked(users)
        epoch = int(user.get("session_epoch") or 0)
    exp = str(int(time.time()) + 14 * 24 * 3600)
    sig = _token_sig(user_id, exp, sid, epoch)
    return f"{user_id}.{exp}.{sid}.{sig}"


def user_from_token(token: str) -> Optional[dict[str, Any]]:
    parts = (token or "").split(".")
    # Three-part cookies from before device ids cannot be revoked individually, so they are refused.
    if len(parts) != 4:
        return None
    user_id, exp, sid, sig = parts
    try:
        if int(exp) < int(time.time()):
            return None
    except ValueError:
        return None
    user = user_by_id(user_id)
    if user is None or not any(row["id"] == sid for row in _sessions(user)):
        return None
    expected = _token_sig(user_id, exp, sid, _epoch_of(user_id))
    if not hmac.compare_digest(sig, expected):
        return None
    return user


def journal_for(user: dict[str, Any]) -> PaperTradeJournal:
    user_id = str(user["id"])
    cached = _BOOKS.get(user_id)
    if cached is not None:
        return cached
    path = guarded_path(_journal_dir() / f"{user_id}.json")
    loaded = _load_journal(path)
    book = loaded if loaded is not None else PaperTradeJournal(equity_0=float(user.get("start_sol") or start_sol_default()))
    book.equity_0 = float(user.get("start_sol") or book.equity_0)
    book.persist_path = path
    _BOOKS[user_id] = book
    return book


def list_users() -> list[dict[str, Any]]:
    return [_public(user) for user in _load()]


def account_row(user: dict[str, Any]) -> dict[str, Any]:
    from app.paper.ledger import excluded_from_go

    book = journal_for(user)
    ranked = [trade for trade in book.closed if not excluded_from_go(trade)]
    realized = sum(float(getattr(trade, "pnl", 0.0) or 0.0) for trade in ranked)
    open_n = 0
    upnl = 0.0
    for lots in book.lots.values():
        for lot in lots:
            qty = float(lot.qty)
            if abs(qty) <= 1e-12:
                continue
            open_n += 1
            upnl += 0.0
    pnl = realized + upnl
    start = max(float(user.get("start_sol") or book.equity_0 or 1.0), 1e-9)
    return {
        **_public(user),
        "pnl": pnl,
        "return_pct": pnl / start,
        "equity": start + pnl,
        "n_closed": len(ranked),
        "open_positions": open_n,
        "day_pnl": user_day_pnl(book),
        "liveEnabled": False,
    }


def leaderboard(sort: str = "pnl") -> dict[str, Any]:
    key = "return_pct" if sort == "return" else "pnl"
    if not auth_enabled():
        rows: list[dict[str, Any]] = []
        note = "本地单用户模式未开启账户。设置 AUU_AUTH=on 后，这里按纸面盈亏排名。系统纸面引擎不计入。"
        note_key = "srv.board.noteLocal"
    else:
        rows = [account_row(user) for user in _load()]
        rows.sort(key=lambda row: float(row.get(key) or 0.0), reverse=True)
        note = "仅用户纸面账户。系统纸面引擎不计入。无充值、无提现、无实盘。"
        note_key = "srv.board.noteAuth"
    return {
        "mode": "paper",
        "liveEnabled": False,
        "liveDisabled": True,
        "auth_enabled": auth_enabled(),
        "sort": key,
        "note": note,
        "noteMsg": {"k": note_key},  # same text, translatable (web: srv.board.*)
        "items": rows,
    }
