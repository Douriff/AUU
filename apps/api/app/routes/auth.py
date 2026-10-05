"""Paper accounts: register, session cookie, leaderboard. No custody."""
from __future__ import annotations

import logging
import os
from typing import Optional

from fastapi import APIRouter, Query, Request

from app.auth.accounts import (
    account_locked,
    account_row,
    clear_login_failures,
    note_login_failure,
    allow_attempt,
    auth_enabled,
    authenticate,
    bump_epoch,
    change_password,
    find_user,
    security_view,
    session_epoch,
    totp_disable,
    totp_enable,
    totp_login,
    totp_new_recovery,
    totp_setup,
    invite_code,
    issue_token,
    journal_for,
    leaderboard,
    list_users,
    locale_view,
    set_locale,
    passwords_match,
    register,
    reset_password,
    email_taken,
    scrub_secrets,
    signup_allowed,
    user_by_email,
    user_by_id,
    user_from_token,
)
from app.auth.email_codes import (
    CodeError,
    email_verify_enabled,
    mask_email,
    normalize_email,
    send_code,
    smtp_configured,
)
from starlette.concurrency import run_in_threadpool
from app.auth import security
from app.paper.books import pop_book, push_book
from app.routes.envelope import err, ok

_LOG = logging.getLogger("auu.auth")
router = APIRouter(prefix="/api/v1", tags=["auth"])

COOKIE = "auu_session"
_MESSAGES = {
    "AUTH_OFF": "登录未开启",
    "SIGNUP_CLOSED": "注册已关闭",
    "INVITE": "邀请码不正确",
    "BAD_NAME": "用户名需为 2–32 位，可用字母、数字、中文和 _ . @ -，须以字母、数字、中文或 _ 开头",
    "BAD_PASSWORD": "密码至少 8 位，必须同时包含字母和数字，可以包含特殊字符（不超过 72 字节）",
    "BAD_DISPLAY": "显示名需为 1–24 位字母、数字、空格或中文",
    "PASSWORD_MISMATCH": "两次密码不一致",
    "BAD_START": "起始 SOL 需在 1 到 100000",
    "NAME_TAKEN": "用户名已存在",
    "RATE_LIMIT": "尝试过于频繁，请稍后再试",
    "BAD_BODY": "请求格式不正确",
    "BAD_LOGIN": "用户名或密码错误",
    "ACCOUNT_LOCKED": "该账户密码错误次数过多，已临时锁定，请 15 分钟后再试或使用忘记密码",
    "EMAIL_OFF": "邮箱验证未开启",
    "EMAIL_NOT_CONFIGURED": "邮件发送尚未配置，请联系管理员",
    "EMAIL_REQUIRED": "请填写邮箱",
    "BAD_EMAIL": "邮箱格式不正确",
    "EMAIL_TAKEN": "该邮箱已被注册，可以直接登录或使用忘记密码",
    "EMAIL_THROTTLE": "验证码发送太频繁，请稍后再试",
    "EMAIL_IP_LIMIT": "当前网络发送验证码次数过多，请 1 小时后再试",
    "EMAIL_SEND_FAILED": "验证码邮件发送失败，请检查邮箱地址或稍后再试",
    "EMAIL_CODE_REQUIRED": "请填写邮箱验证码",
    "EMAIL_CODE_BAD": "验证码不正确",
    "EMAIL_CODE_EXPIRED": "验证码已失效或不存在，请重新获取",
    "EMAIL_CODE_LOCKED": "验证码输错次数过多，已失效，请重新获取",
    "TOTP_REQUIRED": "请输入两步验证码（或恢复码）",
    "TOTP_BAD": "两步验证码不正确或已使用",
    "TOTP_TICKET": "登录步骤已过期，请重新输入密码",
    "TOTP_ALREADY_ON": "两步验证已开启",
    "TOTP_OFF": "两步验证未开启",
    "TOTP_SETUP_EXPIRED": "绑定已过期，请重新开始",
    "BAD_LOCALE": "不支持的语言",
}


class _Bound:
    def __init__(self) -> None:
        self.user: Optional[dict] = None
        self.actor: Optional[dict] = None
        self.token = None
        self.error = None


def session_user(request: Request) -> Optional[dict]:
    if not auth_enabled():
        return None
    return user_from_token(request.cookies.get(COOKIE) or "")


def open_book(request: Request, *, user_id: str = "") -> _Bound:
    """Push the caller's paper journal when AUU_AUTH is on. Auth off leaves the system book."""
    bound = _Bound()
    if not auth_enabled():
        return bound
    actor = session_user(request)
    if actor is None:
        bound.error = err("AUTH_REQUIRED", "请先登录", 401)
        return bound
    target = actor
    wanted = (user_id or "").strip()
    if wanted and wanted != actor.get("id"):
        if not actor.get("is_admin"):
            bound.error = err("FORBIDDEN", "只有管理员可以查看其他账户", 403)
            return bound
        other = user_by_id(wanted)
        if other is None:
            bound.error = err("UNKNOWN_USER", "没有这个账户", 404)
            return bound
        target = other
    bound.actor = actor
    bound.user = target
    bound.token = push_book(journal_for(target))
    return bound


def close_book(bound: _Bound) -> None:
    if bound.token is not None:
        pop_book(bound.token)


def _stamp(response, user_id: str):
    response.set_cookie(
        COOKIE,
        issue_token(user_id),
        httponly=True,
        samesite="lax",
        secure=cookie_secure(),
        path="/",
        max_age=14 * 24 * 3600,
    )
    return response


def cookie_secure() -> bool:
    """Secure flag for the session cookie. Set AUU_COOKIE_SECURE=on when served over HTTPS."""
    return os.getenv("AUU_COOKIE_SECURE", "off").strip().lower() in {"1", "true", "on", "yes"}


def _ip(request: Request) -> str:
    if request.client and request.client.host:
        return request.client.host
    return "local"


async def _json(request: Request) -> dict | object:
    try:
        raw = await request.json()
    except Exception:
        return err("BAD_BODY", _MESSAGES["BAD_BODY"], 400)
    if not isinstance(raw, dict):
        return err("BAD_BODY", _MESSAGES["BAD_BODY"], 400)
    return raw


def _text(raw: dict, key: str) -> str:
    value = raw.get(key)
    return value if isinstance(value, str) else ""


def _start_sol(raw: dict) -> Optional[float]:
    if "start_sol" not in raw or raw.get("start_sol") in (None, ""):
        return None
    value = raw.get("start_sol")
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError("BAD_START")
    try:
        return float(value)
    except ValueError as exc:
        raise ValueError("BAD_START") from exc


def _fail(exc: ValueError, action: str = "auth"):
    code = str(exc)
    _LOG.warning("%s rejected: %s", action, code)
    status = 401 if code in {"BAD_LOGIN", "TOTP_BAD", "TOTP_TICKET"} else 400
    if code in {"EMAIL_THROTTLE", "EMAIL_IP_LIMIT"}:
        status = 429
    if code == "EMAIL_SEND_FAILED":
        status = 502
    message = _MESSAGES.get(code, "请求无效")
    wait = int(getattr(exc, "retry_after", 0) or 0)
    if code == "EMAIL_THROTTLE" and wait > 0:
        message = f"验证码发送太频繁，请 {wait} 秒后再试"
        return err(code, message, status, extra={"retry_after": wait})
    return err(code, message, status)


def _me_payload(user: Optional[dict]) -> dict:
    body = {
        "auth_enabled": auth_enabled(),
        "signup_allowed": signup_allowed(),
        "invite_required": bool(invite_code()),
        "email_verify": bool(auth_enabled() and email_verify_enabled()),
        "user": None,
        "liveEnabled": False,
        "liveDisabled": True,
        "mode": "paper",
    }
    if user is not None:
        body["user"] = {**account_row(user), **locale_view(user)}
    return body


@router.post("/auth/register")
async def auth_register(request: Request):
    if not allow_attempt(f"signup:{_ip(request)}"):
        return err("RATE_LIMIT", _MESSAGES["RATE_LIMIT"], 429)
    raw = await _json(request)
    if not isinstance(raw, dict):
        return raw
    password = _text(raw, "password")
    if not passwords_match(password, _text(raw, "password_confirm")):
        _LOG.warning("register rejected: PASSWORD_MISMATCH")
        return err("PASSWORD_MISMATCH", _MESSAGES["PASSWORD_MISMATCH"], 400)
    try:
        user = register(
            _text(raw, "name"),
            password,
            _text(raw, "invite"),
            _start_sol(raw),
            _text(raw, "display_name"),
            _text(raw, "email"),
            _text(raw, "email_code"),
        )
    except ValueError as exc:
        return _fail(exc, "register")
    return _stamp(ok(scrub_secrets(_me_payload(user_by_id(user["id"])))), user["id"])


@router.post("/auth/email/code")
async def auth_email_code(request: Request):
    """Send a 6-digit code for signup or password reset. Never echoes the code or full address."""
    if not auth_enabled():
        return err("AUTH_OFF", _MESSAGES["AUTH_OFF"], 400)
    if not email_verify_enabled():
        return err("EMAIL_OFF", _MESSAGES["EMAIL_OFF"], 400)
    if not smtp_configured():
        _LOG.warning("email code rejected: EMAIL_NOT_CONFIGURED")
        return err("EMAIL_NOT_CONFIGURED", _MESSAGES["EMAIL_NOT_CONFIGURED"], 503)
    raw = await _json(request)
    if not isinstance(raw, dict):
        return raw
    purpose = _text(raw, "purpose") or "signup"
    if purpose not in {"signup", "reset"}:
        return err("BAD_BODY", _MESSAGES["BAD_BODY"], 400)
    address = normalize_email(_text(raw, "email"))
    if not address:
        return err("EMAIL_REQUIRED", _MESSAGES["EMAIL_REQUIRED"], 400)
    deliver = True
    if purpose == "signup":
        if not signup_allowed():
            return err("SIGNUP_CLOSED", _MESSAGES["SIGNUP_CLOSED"], 400)
        if email_taken(address):
            return _fail(ValueError("EMAIL_TAKEN"), "email code")
    else:
        # Same answer whether or not the address is registered; only registered ones get mail.
        deliver = email_taken(address)
    try:
        await run_in_threadpool(send_code, purpose, address, _ip(request), deliver=deliver, lang=_text(raw, "lang") or "zh-CN")
    except CodeError as exc:
        return _fail(exc, "email code")
    note = "验证码已发送，请查收邮件（10 分钟内有效）"
    if purpose == "reset":
        note = "如果该邮箱已注册，验证码已发送，请查收邮件（10 分钟内有效）"
    return ok({"sent": True, "email": mask_email(address), "ttl_sec": 600, "resend_after_sec": 60, "message": note, "liveEnabled": False})


@router.post("/auth/password/reset")
async def auth_password_reset(request: Request):
    if not auth_enabled():
        return err("AUTH_OFF", _MESSAGES["AUTH_OFF"], 400)
    if not allow_attempt(f"reset:{_ip(request)}"):
        return err("RATE_LIMIT", _MESSAGES["RATE_LIMIT"], 429)
    raw = await _json(request)
    if not isinstance(raw, dict):
        return raw
    new = _text(raw, "new_password")
    if not passwords_match(new, _text(raw, "new_password_confirm")):
        return err("PASSWORD_MISMATCH", _MESSAGES["PASSWORD_MISMATCH"], 400)
    try:
        reset_password(_text(raw, "email"), _text(raw, "code"), new)
    except ValueError as exc:
        return _fail(exc, "password reset")
    response = ok(scrub_secrets({"ok": True, "message": "密码已重置，请使用新密码登录", "liveEnabled": False, "mode": "paper"}))
    response.delete_cookie(COOKIE, path="/")
    return response


@router.post("/auth/login")
async def auth_login(request: Request):
    raw = await _json(request)
    if not isinstance(raw, dict):
        return raw
    name = _text(raw, "name")
    if "@" in name and find_user(name) is None:  # username first; else an email -> its account name
        hit = user_by_email(name)
        name = str(hit["name"]) if hit is not None else name
    if not allow_attempt(f"login:{name.strip().lower()}:{_ip(request)}"):
        return err("RATE_LIMIT", _MESSAGES["RATE_LIMIT"], 429)
    if not auth_enabled():
        return err("AUTH_OFF", _MESSAGES["AUTH_OFF"], 400)
    if account_locked(name):
        _LOG.warning("login rejected: ACCOUNT_LOCKED")
        return err("ACCOUNT_LOCKED", _MESSAGES["ACCOUNT_LOCKED"], 429)
    user = authenticate(name, _text(raw, "password"))
    if user is None:
        note_login_failure(name)
        known = find_user(name)
        if known is not None:
            _record(request, known["id"], "bad_password", "password")
        _LOG.warning("login rejected: BAD_LOGIN")
        return err("BAD_LOGIN", _MESSAGES["BAD_LOGIN"], 401)
    if security.enabled(user):
        # Password ok; the session cookie is only issued after the TOTP / recovery step.
        _record(request, user["id"], "password_ok_2fa_pending", "password")
        return ok({"totp_required": True, "ticket": security.issue_ticket(user["id"], session_epoch(user["id"])),
                   "ticket_ttl_sec": security.TICKET_TTL, "liveEnabled": False, "mode": "paper"})
    clear_login_failures(name)
    _record(request, user["id"], "ok", "password")
    return _stamp(ok(scrub_secrets(_me_payload(user))), user["id"])


def _record(request: Request, user_id: str, result: str, method: str) -> None:
    try:
        security.login_log().add(user_id, _ip(request), request.headers.get("user-agent", ""), result, method)
    except Exception:  # a broken log must never block a login
        _LOG.exception("login record failed")


@router.post("/auth/login/totp")
async def auth_login_totp(request: Request):
    """Second step: the ticket from /auth/login plus a 6-digit TOTP or a one-time recovery code."""
    if not auth_enabled():
        return err("AUTH_OFF", _MESSAGES["AUTH_OFF"], 400)
    raw = await _json(request)
    if not isinstance(raw, dict):
        return raw
    uid = security.read_ticket(_text(raw, "ticket"), session_epoch)
    user = user_by_id(uid) if uid else None
    if user is None or not security.enabled(user):
        return err("TOTP_TICKET", _MESSAGES["TOTP_TICKET"], 401)
    name = str(user.get("name") or "")
    if not allow_attempt(f"totp:{uid}:{_ip(request)}"):
        return err("RATE_LIMIT", _MESSAGES["RATE_LIMIT"], 429)
    if account_locked(name):
        return err("ACCOUNT_LOCKED", _MESSAGES["ACCOUNT_LOCKED"], 429)
    how = totp_login(uid, _text(raw, "code"))
    if how is None:
        note_login_failure(name)
        _record(request, uid, "bad_2fa", "totp")
        _LOG.warning("login rejected: TOTP_BAD")
        return err("TOTP_BAD", _MESSAGES["TOTP_BAD"], 401)
    clear_login_failures(name)
    _record(request, uid, "ok", how)
    body = _me_payload(user_by_id(uid))
    if how == "recovery":
        body["notice"] = f"已使用一个恢复码，剩余 {security_view(user_by_id(uid))['recovery_left']} 个"
    return _stamp(ok(scrub_secrets(body)), uid)


def _actor(request: Request):
    if not auth_enabled():
        return None, err("AUTH_OFF", _MESSAGES["AUTH_OFF"], 400)
    actor = session_user(request)
    if actor is None:
        return None, err("AUTH_REQUIRED", "请先登录", 401)
    return actor, None


@router.get("/auth/security")
def auth_security(request: Request):
    actor, bad = _actor(request)
    if bad is not None:
        return bad
    logins = security.login_log().recent(str(actor["id"]), 20)
    return ok({**security_view(actor), "logins": logins, "liveEnabled": False,
               "sessions_note": "会话是签名 Cookie（14 天）。“退出其他会话”会让其他设备的登录立即失效，当前设备保持登录。"})


async def _sec_body(request: Request, actor) -> dict | object:
    if not allow_attempt(f"security:{actor['id']}"):
        return err("RATE_LIMIT", _MESSAGES["RATE_LIMIT"], 429)
    return await _json(request)


@router.post("/auth/2fa/setup")
async def auth_2fa_setup(request: Request):
    actor, bad = _actor(request)
    if bad is not None:
        return bad
    raw = await _sec_body(request, actor)
    if not isinstance(raw, dict):
        return raw
    try:
        out = totp_setup(str(actor["id"]), _text(raw, "password"))
    except ValueError as exc:
        return _fail(exc, "2fa setup")
    return ok({**out, "liveEnabled": False})


@router.post("/auth/2fa/enable")
async def auth_2fa_enable(request: Request):
    actor, bad = _actor(request)
    if bad is not None:
        return bad
    raw = await _sec_body(request, actor)
    if not isinstance(raw, dict):
        return raw
    try:
        codes = totp_enable(str(actor["id"]), _text(raw, "code"))
    except ValueError as exc:
        return _fail(exc, "2fa enable")
    _record(request, str(actor["id"]), "2fa_enabled", "settings")
    return ok({"enabled": True, "recovery_codes": codes, "liveEnabled": False,
               "message": "两步验证已开启。恢复码只显示这一次，请离线保存；每个只能用一次。"})


@router.post("/auth/2fa/disable")
async def auth_2fa_disable(request: Request):
    actor, bad = _actor(request)
    if bad is not None:
        return bad
    raw = await _sec_body(request, actor)
    if not isinstance(raw, dict):
        return raw
    try:
        totp_disable(str(actor["id"]), _text(raw, "password"), _text(raw, "code"))
    except ValueError as exc:
        return _fail(exc, "2fa disable")
    _record(request, str(actor["id"]), "2fa_disabled", "settings")
    return ok({"enabled": False, "liveEnabled": False})


@router.post("/auth/2fa/recovery")
async def auth_2fa_recovery(request: Request):
    actor, bad = _actor(request)
    if bad is not None:
        return bad
    raw = await _sec_body(request, actor)
    if not isinstance(raw, dict):
        return raw
    try:
        codes = totp_new_recovery(str(actor["id"]), _text(raw, "password"), _text(raw, "code"))
    except ValueError as exc:
        return _fail(exc, "2fa recovery")
    _record(request, str(actor["id"]), "recovery_regenerated", "settings")
    return ok({"recovery_codes": codes, "liveEnabled": False, "message": "旧恢复码已全部作废。新恢复码只显示这一次。"})


@router.post("/auth/sessions/revoke-others")
async def auth_revoke_others(request: Request):
    actor, bad = _actor(request)
    if bad is not None:
        return bad
    if not allow_attempt(f"security:{actor['id']}"):
        return err("RATE_LIMIT", _MESSAGES["RATE_LIMIT"], 429)
    bump_epoch(str(actor["id"]))
    _record(request, str(actor["id"]), "revoked_other_sessions", "settings")
    return _stamp(ok({"ok": True, "liveEnabled": False, "message": "其他设备的登录已失效"}), str(actor["id"]))


@router.post("/auth/password")
async def auth_password(request: Request):
    if not auth_enabled():
        return err("AUTH_OFF", _MESSAGES["AUTH_OFF"], 400)
    actor = session_user(request)
    if actor is None:
        return err("AUTH_REQUIRED", "请先登录", 401)
    if not allow_attempt(f"password:{actor['id']}"):
        return err("RATE_LIMIT", _MESSAGES["RATE_LIMIT"], 429)
    raw = await _json(request)
    if not isinstance(raw, dict):
        return raw
    new = _text(raw, "new_password")
    if not passwords_match(new, _text(raw, "new_password_confirm")):
        return err("PASSWORD_MISMATCH", _MESSAGES["PASSWORD_MISMATCH"], 400)
    try:
        change_password(str(actor["id"]), _text(raw, "current_password"), new, _text(raw, "totp_code"))
    except ValueError as exc:
        return _fail(exc, "password change")
    _record(request, str(actor["id"]), "password_changed", "settings")
    # Other sessions end (session_epoch bumped); this device gets a fresh cookie.
    return _stamp(ok(scrub_secrets({"ok": True, "liveEnabled": False, "mode": "paper"})), str(actor["id"]))


@router.put("/auth/locale")
async def auth_locale(request: Request):
    """Save the interface language on the account so other devices follow it."""
    actor, bad = _actor(request)
    if bad is not None:
        return bad
    if not allow_attempt(f"locale:{actor['id']}"):
        return err("RATE_LIMIT", _MESSAGES["RATE_LIMIT"], 429)
    raw = await _json(request)
    if not isinstance(raw, dict):
        return raw
    try:
        view = set_locale(str(actor["id"]), _text(raw, "locale"))
    except ValueError as exc:
        return _fail(exc, "locale")
    return ok({**view, "liveEnabled": False})


@router.post("/auth/logout")
def auth_logout():
    response = ok({"ok": True, "liveEnabled": False})
    response.delete_cookie(COOKIE, path="/")
    return response


@router.get("/auth/me")
def auth_me(request: Request):
    return ok(scrub_secrets(_me_payload(session_user(request))))


@router.get("/auth/users")
def auth_users(request: Request):
    if not auth_enabled():
        return err("AUTH_OFF", _MESSAGES["AUTH_OFF"], 400)
    actor = session_user(request)
    if actor is None:
        return err("AUTH_REQUIRED", "请先登录", 401)
    if not actor.get("is_admin"):
        return err("FORBIDDEN", "只有管理员可以查看全部账户", 403)
    return ok(
        scrub_secrets(
            {
                "mode": "paper",
                "liveEnabled": False,
                "items": [account_row(user_by_id(row["id"])) for row in list_users() if user_by_id(row["id"])],
            }
        )
    )


@router.get("/leaderboard")
def auth_leaderboard(sort: str = Query("pnl")):
    key = "return" if sort in {"return", "return_pct"} else "pnl"
    return ok(scrub_secrets(leaderboard(key)))
