"""Email verification codes for signup and password reset.

Config comes only from env. The SMTP password, the codes and full addresses are
never logged; only masked addresses (a***@qq.com) appear in logs.
Codes live in process memory as salted HMAC hashes and expire after 10 minutes.
"""
from __future__ import annotations

import hmac
import logging
import os
import re
import secrets
import smtplib
import ssl
import threading
import time
from email.message import EmailMessage
from email.utils import formataddr
from hashlib import sha256
from typing import Any, Callable, Optional

_LOG = logging.getLogger("auu.email")
_EMAIL = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(\.[A-Za-z0-9\-]+)+$")
PURPOSES = ("signup", "reset")
CODE_TTL = 600.0
RESEND_GAP = 60.0
MAX_WRONG = 5

_LOCK = threading.RLock()
_CODES: dict[tuple[str, str], dict[str, Any]] = {}
_IP_SENDS: dict[str, list[float]] = {}
Sender = Callable[[str, str, str], None]
_SENDER: Optional[Sender] = None


class CodeError(ValueError):
    """ValueError whose str() is the API error code; retry_after is set for throttles."""

    def __init__(self, code: str, retry_after: int = 0) -> None:
        super().__init__(code)
        self.retry_after = retry_after


def _now() -> float:
    return time.time()


def _truthy(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "on", "yes"}


def email_verify_enabled() -> bool:
    return _truthy(os.getenv("AUU_EMAIL_VERIFY", "off"))


def smtp_settings() -> dict[str, Any]:
    try:
        port = int(os.getenv("AUU_SMTP_PORT", "465"))
    except ValueError:
        port = 465
    return {
        "host": (os.getenv("AUU_SMTP_HOST") or "smtp.qq.com").strip(),
        "port": port,
        "user": (os.getenv("AUU_SMTP_USER") or "").strip(),
        "password": os.getenv("AUU_SMTP_PASS") or "",
    }


def smtp_configured() -> bool:
    cfg = smtp_settings()
    return bool(cfg["host"] and cfg["user"] and cfg["password"])


def _ip_max() -> int:
    try:
        return max(1, int(os.getenv("AUU_EMAIL_IP_MAX", "10")))
    except ValueError:
        return 10


def _ip_window() -> float:
    try:
        return max(1.0, float(os.getenv("AUU_EMAIL_IP_WINDOW", "3600")))
    except ValueError:
        return 3600.0


def normalize_email(raw: str) -> str:
    return (raw or "").strip().lower()


def email_ok(email: str) -> bool:
    return 3 <= len(email) <= 254 and bool(_EMAIL.match(email))


def mask_email(email: str) -> str:
    local, _, domain = (email or "").partition("@")
    if not domain:
        return "***"
    return f"{local[:1]}***@{domain}"


def set_sender(sender: Optional[Sender]) -> None:
    """Tests install a fake sender here. None restores real SMTP."""
    global _SENDER
    _SENDER = sender


def reset_email_codes() -> None:
    with _LOCK:
        _CODES.clear()
        _IP_SENDS.clear()


def _smtp_send(to: str, subject: str, body: str) -> None:
    cfg = smtp_settings()
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr(("AUU 纸面交易", cfg["user"]))
    msg["To"] = to
    msg.set_content(body)
    context = ssl.create_default_context()
    if cfg["port"] == 465:
        with smtplib.SMTP_SSL(cfg["host"], cfg["port"], timeout=15, context=context) as client:
            client.login(cfg["user"], cfg["password"])
            client.send_message(msg)
    else:
        with smtplib.SMTP(cfg["host"], cfg["port"], timeout=15) as client:
            client.starttls(context=context)
            client.login(cfg["user"], cfg["password"])
            client.send_message(msg)


def _digest(purpose: str, email: str, code: str, salt: str) -> str:
    key = (os.getenv("AUU_SESSION_SECRET", "auu-paper-dev") + ":email-code").encode("utf-8")
    return hmac.new(key, f"{purpose}:{email}:{salt}:{code}".encode("utf-8"), sha256).hexdigest()


def _body(purpose: str, code: str) -> tuple[str, str]:
    if purpose == "reset":
        subject = "AUU 重置密码验证码"
        action = "重置密码"
    else:
        subject = "AUU 注册验证码"
        action = "注册账户"
    body = (
        f"你正在{action}，验证码：{code}\n\n"
        "验证码 10 分钟内有效，最多可输错 5 次。\n"
        "如果这不是你本人的操作，请忽略这封邮件。\n\n"
        "AUU 只做纸面交易，不会向你索要私钥或转账。"
    )
    return subject, body


def send_code(purpose: str, email: str, ip: str, *, deliver: bool = True) -> None:
    """Create and email a 6-digit code. deliver=False applies the same throttles without sending
    (used for reset requests to unknown addresses so responses do not reveal registration)."""
    if purpose not in PURPOSES:
        raise CodeError("BAD_BODY")
    if not email_ok(email):
        raise CodeError("BAD_EMAIL")
    now = _now()
    key = (purpose, email)
    with _LOCK:
        row = _CODES.get(key)
        if row and now - row["sent_at"] < RESEND_GAP:
            raise CodeError("EMAIL_THROTTLE", int(RESEND_GAP - (now - row["sent_at"])) + 1)
        sends = [stamp for stamp in _IP_SENDS.get(ip, []) if now - stamp < _ip_window()]
        if len(sends) >= _ip_max():
            _IP_SENDS[ip] = sends
            raise CodeError("EMAIL_IP_LIMIT")
        sends.append(now)
        _IP_SENDS[ip] = sends
        code = f"{secrets.randbelow(1_000_000):06d}"
        salt = secrets.token_hex(8)
        _CODES[key] = {
            "hash": _digest(purpose, email, code, salt),
            "salt": salt,
            "sent_at": now,
            "expires": now + CODE_TTL,
            "wrong": 0,
            "live": deliver,
        }
    if not deliver:
        _LOG.info("email code (%s) skipped for %s", purpose, mask_email(email))
        return
    subject, body = _body(purpose, code)
    try:
        (_SENDER or _smtp_send)(email, subject, body)
    except Exception as exc:  # never include exc text: SMTP errors can echo addresses
        with _LOCK:
            _CODES.pop(key, None)
        _LOG.warning("email code (%s) send failed for %s: %s", purpose, mask_email(email), type(exc).__name__)
        raise CodeError("EMAIL_SEND_FAILED") from None
    _LOG.info("email code (%s) sent to %s", purpose, mask_email(email))


def check_code(purpose: str, email: str, code: str, *, consume: bool = True) -> None:
    """Raise CodeError unless the code matches. Wrong guesses count toward the lockout."""
    text = (code or "").strip()
    if not text:
        raise CodeError("EMAIL_CODE_REQUIRED")
    now = _now()
    key = (purpose, email)
    with _LOCK:
        row = _CODES.get(key)
        if row is None or now > row["expires"] or not row.get("live", True):
            if row is not None and now > row["expires"]:
                _CODES.pop(key, None)
            raise CodeError("EMAIL_CODE_EXPIRED")
        if row["wrong"] >= MAX_WRONG:
            raise CodeError("EMAIL_CODE_LOCKED")
        good = re.fullmatch(r"\d{6}", text) is not None and hmac.compare_digest(
            _digest(purpose, email, text, row["salt"]), row["hash"]
        )
        if not good:
            row["wrong"] += 1
            if row["wrong"] >= MAX_WRONG:
                _LOG.warning("email code (%s) locked for %s", purpose, mask_email(email))
                raise CodeError("EMAIL_CODE_LOCKED")
            raise CodeError("EMAIL_CODE_BAD")
        if consume:
            _CODES.pop(key, None)
