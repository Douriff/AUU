"""Local paper accounts. Passwords are bcrypt hashes. No custody of funds."""
from __future__ import annotations

import hmac
import json
import os
import re
import threading
import time
import uuid
from hashlib import sha256
from pathlib import Path
from typing import Any, Optional

import bcrypt

from app.data_paths import data_dir, guarded_path
from app.paper.books import user_day_pnl
from app.paper.ledger import PaperTradeJournal, _load_journal

_NAME = re.compile(r"^[A-Za-z0-9_\u4e00-\u9fff]{2,24}$")
_DISPLAY = re.compile(r"^[A-Za-z0-9_\u4e00-\u9fff][A-Za-z0-9_ \u4e00-\u9fff]{0,23}$")
_LOCK = threading.RLock()
_USERS: Optional[list[dict[str, Any]]] = None
_BOOKS: dict[str, PaperTradeJournal] = {}
_ATTEMPTS: dict[str, list[float]] = {}
_HIDDEN = {"password", "password_hash", "password_confirm", "current_password", "new_password", "new_password_confirm", "invite"}


def reset_accounts() -> None:
    global _USERS
    with _LOCK:
        _USERS = None
        _BOOKS.clear()
        _ATTEMPTS.clear()


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


def _load() -> list[dict[str, Any]]:
    global _USERS
    with _LOCK:
        if _USERS is not None:
            return _USERS
        path = _store_path()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raw = {"users": []}
        rows = raw.get("users") if isinstance(raw, dict) else []
        _USERS = [row for row in rows if isinstance(row, dict) and row.get("id")]
        return _USERS


def _save() -> None:
    path = guarded_path(_store_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"users": _load()}
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    tmp.replace(path)


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
    }


def scrub_secrets(payload: Any) -> Any:
    """Drop password material before a response leaves the process."""
    if isinstance(payload, dict):
        return {key: scrub_secrets(value) for key, value in payload.items() if key not in _HIDDEN}
    if isinstance(payload, list):
        return [scrub_secrets(item) for item in payload]
    return payload


def _password_ok(password: str) -> bool:
    raw = password or ""
    if len(raw) < 8:
        return False
    return len(raw.encode("utf-8")) <= 72


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
    users = _load()
    if any(str(row.get("name") or "").lower() == text.lower() for row in users):
        raise ValueError("NAME_TAKEN")
    admin_env = os.getenv("AUU_ADMIN_USER", "").strip()
    is_admin = not users or (bool(admin_env) and text == admin_env)
    user = {
        "id": uuid.uuid4().hex,
        "name": text,
        "display_name": shown,
        "password_hash": hash_password(password),
        "start_sol": start,
        "is_admin": is_admin,
        "created_ts": int(time.time() * 1000),
    }
    with _LOCK:
        users.append(user)
        _save()
    return _public(user)


def authenticate(name: str, password: str) -> Optional[dict[str, Any]]:
    text = (name or "").strip().lower()
    for user in _load():
        if str(user.get("name") or "").lower() == text and verify_password(password, str(user.get("password_hash") or "")):
            return user
    return None


def change_password(user_id: str, current: str, new: str) -> None:
    user = user_by_id(user_id)
    if user is None or not verify_password(current or "", str(user.get("password_hash") or "")):
        raise ValueError("BAD_LOGIN")
    if not _password_ok(new):
        raise ValueError("BAD_PASSWORD")
    user["password_hash"] = hash_password(new)
    with _LOCK:
        _save()


def user_by_id(user_id: str) -> Optional[dict[str, Any]]:
    for user in _load():
        if user.get("id") == user_id:
            return user
    return None


def issue_token(user_id: str) -> str:
    exp = int(time.time()) + 14 * 24 * 3600
    payload = f"{user_id}.{exp}".encode("utf-8")
    sig = hmac.new(_session_secret(), payload, sha256).hexdigest()
    return f"{user_id}.{exp}.{sig}"


def user_from_token(token: str) -> Optional[dict[str, Any]]:
    parts = (token or "").split(".")
    if len(parts) != 3:
        return None
    user_id, exp, sig = parts
    try:
        if int(exp) < int(time.time()):
            return None
    except ValueError:
        return None
    payload = f"{user_id}.{exp}".encode("utf-8")
    expected = hmac.new(_session_secret(), payload, sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None
    return user_by_id(user_id)


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
    book = journal_for(user)
    realized = sum(float(getattr(trade, "pnl", 0.0) or 0.0) for trade in book.closed)
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
        "n_closed": len(book.closed),
        "open_positions": open_n,
        "day_pnl": user_day_pnl(book),
        "liveEnabled": False,
    }


def leaderboard(sort: str = "pnl") -> dict[str, Any]:
    key = "return_pct" if sort == "return" else "pnl"
    if not auth_enabled():
        rows: list[dict[str, Any]] = []
        note = "本地单用户模式未开启账户。设置 AUU_AUTH=on 后，这里按纸面盈亏排名。系统纸面引擎不计入。"
    else:
        rows = [account_row(user) for user in _load()]
        rows.sort(key=lambda row: float(row.get(key) or 0.0), reverse=True)
        note = "仅用户纸面账户。系统纸面引擎不计入。无充值、无提现、无实盘。"
    return {
        "mode": "paper",
        "liveEnabled": False,
        "liveDisabled": True,
        "auth_enabled": auth_enabled(),
        "sort": key,
        "note": note,
        "items": rows,
    }
