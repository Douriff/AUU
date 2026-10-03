"""Email verification codes for signup and password reset.

Config comes only from env. The SMTP password, the codes and full addresses are
never logged; only masked addresses (a***@qq.com) appear in logs.
Codes live in process memory as salted HMAC hashes and expire after 10 minutes.
"""
from __future__ import annotations

import hmac
import html
import logging
import os
import re
import secrets
import smtplib
import ssl
import threading
import time
import unicodedata
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
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


# Invisible/format characters (U+200B zero-width space, U+FEFF BOM, U+2060 word joiner...)
# and space separators (U+00A0 no-break space, U+3000 ideographic space...) often ride
# along when an App Password is copied from a web page or chat app.
_INVISIBLE_CATEGORIES = frozenset({"Cf", "Zs", "Zl", "Zp"})


def clean_smtp_password(raw: Optional[str]) -> str:
    """Drop all whitespace and invisible/format characters from an SMTP password.

    Google shows App Passwords as "abcd efgh ijkl mnop"; QQ/163 codes have no spaces
    either way, so nothing a real password needs is removed."""
    return "".join(
        ch for ch in (raw or "") if not ch.isspace() and unicodedata.category(ch) not in _INVISIBLE_CATEGORIES
    )


def smtp_settings() -> dict[str, Any]:
    """Presets: Gmail smtp.gmail.com 465 (SSL) or 587 (STARTTLS) with a Google App Password;
    QQ smtp.qq.com 465; 163 smtp.163.com 465. Port 465 means implicit SSL, any other port
    means STARTTLS, unless AUU_SMTP_STARTTLS=on/off says otherwise."""
    try:
        port = int(os.getenv("AUU_SMTP_PORT", "465"))
    except ValueError:
        port = 465
    flag = (os.getenv("AUU_SMTP_STARTTLS") or "").strip().lower()
    if flag in {"1", "true", "on", "yes"}:
        starttls = True
    elif flag in {"0", "false", "off", "no"}:
        starttls = False
    else:
        starttls = port != 465
    password = clean_smtp_password(os.getenv("AUU_SMTP_PASS") or "")
    return {
        "host": (os.getenv("AUU_SMTP_HOST") or "smtp.qq.com").strip(),
        "port": port,
        "starttls": starttls,
        "user": (os.getenv("AUU_SMTP_USER") or "").strip(),
        "password": password,
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


BRAND = "AUUTRADE"


def _smtp_send(to: str, subject: str, body: str, html_body: Optional[str] = None) -> dict[str, Any]:
    """Send plain text, or multipart/alternative (text + HTML) when html_body is given.

    Returns {"message_id", "response"} where response is the server's reply to DATA
    (e.g. "250 2.0.0 OK ... - gsmtp"), kept as delivery evidence."""
    cfg = smtp_settings()
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((BRAND, cfg["user"]))
    msg["To"] = to
    msg["Date"] = formatdate(localtime=True)
    domain = cfg["user"].rpartition("@")[2] or None
    msg["Message-ID"] = make_msgid(domain=domain)
    msg["Auto-Submitted"] = "auto-generated"
    msg.set_content(body)
    if html_body:
        msg.add_alternative(html_body, subtype="html")
    context = ssl.create_default_context()
    out: dict[str, Any] = {"message_id": msg["Message-ID"], "response": None}

    def _send(client) -> None:
        orig = client.data

        def data(m):
            code, resp = orig(m)
            out["response"] = f"{code} {resp.decode('utf-8', 'replace') if isinstance(resp, bytes) else resp}"[:200]
            return code, resp

        client.data = data
        client.send_message(msg)

    if not cfg["starttls"]:
        with smtplib.SMTP_SSL(cfg["host"], cfg["port"], timeout=15, context=context) as client:
            client.login(cfg["user"], cfg["password"])
            _send(client)
    else:
        with smtplib.SMTP(cfg["host"], cfg["port"], timeout=15) as client:
            client.ehlo()
            client.starttls(context=context)
            client.ehlo()
            client.login(cfg["user"], cfg["password"])
            _send(client)
    return out


def _digest(purpose: str, email: str, code: str, salt: str) -> str:
    key = (os.getenv("AUU_SESSION_SECRET", "auu-paper-dev") + ":email-code").encode("utf-8")
    return hmac.new(key, f"{purpose}:{email}:{salt}:{code}".encode("utf-8"), sha256).hexdigest()


_TEMPLATES = {
    "signup": {
        "subject": "AUUTRADE 注册验证码：{code}",
        "action": "您正在注册 AUUTRADE 账号，本次验证码为：",
        "ignore": "如果这不是您本人的操作，请忽略本邮件，您的邮箱不会被绑定。",
    },
    "reset": {
        "subject": "AUUTRADE 重置密码验证码：{code}",
        "action": "您正在重置 AUUTRADE 账号的登录密码，本次验证码为：",
        "ignore": "如果这不是您本人的操作，请忽略本邮件，并建议尽快修改邮箱密码。",
    },
}


def _ttl_minutes() -> int:
    return max(1, int(CODE_TTL // 60))


def render_email(purpose: str, code: str) -> tuple[str, str, str]:
    """Return (subject, plain text, HTML) for a verification code email.

    The HTML uses inline styles only: no external images, fonts, links or tracking pixels."""
    tpl = _TEMPLATES["reset" if purpose == "reset" else "signup"]
    subject = tpl["subject"].format(code=code)
    validity = f"验证码 {_ttl_minutes()} 分钟内有效，请勿泄露给任何人。{BRAND} 工作人员不会以任何理由向您索要验证码。"
    text = (
        "您好，\n\n"
        f"{tpl['action']}\n\n"
        f"{code}\n\n"
        f"{validity}\n"
        f"{tpl['ignore']}\n\n"
        f"—— {BRAND} 团队\n\n"
        "此邮件由系统自动发送，请勿直接回复。\n"
    )
    esc = html.escape
    font = "-apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC','Microsoft YaHei',Arial,sans-serif"
    html_body = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{esc(subject)}</title></head>
<body style="margin:0;padding:0;background:#f3f4f6;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f3f4f6;padding:32px 12px;font-family:{font};">
<tr><td align="center">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:520px;background:#ffffff;border-radius:8px;overflow:hidden;border:1px solid #e5e7eb;">
<tr><td style="background:#0f172a;padding:20px 32px;">
<span style="color:#ffffff;font-size:22px;font-weight:700;letter-spacing:4px;">{BRAND}</span>
</td></tr>
<tr><td style="padding:32px;color:#111827;font-size:15px;line-height:1.7;">
<p style="margin:0 0 12px;">您好，</p>
<p style="margin:0 0 20px;">{esc(tpl['action'])}</p>
<div style="margin:0 0 24px;padding:18px 0;background:#f8fafc;border:1px solid #e2e8f0;border-radius:6px;text-align:center;">
<span style="font-size:34px;font-weight:700;letter-spacing:10px;color:#0f172a;font-family:Consolas,'SFMono-Regular',Menlo,monospace;">{esc(code)}</span>
</div>
<p style="margin:0 0 12px;color:#374151;">{esc(validity)}</p>
<p style="margin:0 0 24px;color:#374151;">{esc(tpl['ignore'])}</p>
<p style="margin:0;color:#111827;">—— {BRAND} 团队</p>
</td></tr>
<tr><td style="padding:16px 32px;background:#f9fafb;border-top:1px solid #e5e7eb;color:#9ca3af;font-size:12px;line-height:1.6;">
此邮件由系统自动发送，请勿直接回复。<br>&copy; {BRAND}
</td></tr>
</table>
</td></tr>
</table>
</body>
</html>
"""
    return subject, text, html_body


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
    subject, body, html_body = render_email(purpose, code)
    try:
        if _SENDER is not None:
            _SENDER(email, subject, body)
        else:
            _smtp_send(email, subject, body, html_body)
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
