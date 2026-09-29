"""Paper accounts: register, session cookie, leaderboard. No custody."""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Query, Request

from app.auth.accounts import (
    account_row,
    allow_attempt,
    auth_enabled,
    authenticate,
    change_password,
    invite_code,
    issue_token,
    journal_for,
    leaderboard,
    list_users,
    passwords_match,
    register,
    scrub_secrets,
    signup_allowed,
    user_by_id,
    user_from_token,
)
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
        path="/",
        max_age=14 * 24 * 3600,
    )
    return response


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
    status = 401 if code == "BAD_LOGIN" else 400
    return err(code, _MESSAGES.get(code, "请求无效"), status)


def _me_payload(user: Optional[dict]) -> dict:
    body = {
        "auth_enabled": auth_enabled(),
        "signup_allowed": signup_allowed(),
        "invite_required": bool(invite_code()),
        "user": None,
        "liveEnabled": False,
        "liveDisabled": True,
        "mode": "paper",
    }
    if user is not None:
        body["user"] = account_row(user)
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
        )
    except ValueError as exc:
        return _fail(exc, "register")
    return _stamp(ok(scrub_secrets(_me_payload(user_by_id(user["id"])))), user["id"])


@router.post("/auth/login")
async def auth_login(request: Request):
    raw = await _json(request)
    if not isinstance(raw, dict):
        return raw
    name = _text(raw, "name")
    if not allow_attempt(f"login:{name.strip().lower()}:{_ip(request)}"):
        return err("RATE_LIMIT", _MESSAGES["RATE_LIMIT"], 429)
    if not auth_enabled():
        return err("AUTH_OFF", _MESSAGES["AUTH_OFF"], 400)
    user = authenticate(name, _text(raw, "password"))
    if user is None:
        _LOG.warning("login rejected: BAD_LOGIN")
        return err("BAD_LOGIN", _MESSAGES["BAD_LOGIN"], 401)
    return _stamp(ok(scrub_secrets(_me_payload(user))), user["id"])


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
        change_password(str(actor["id"]), _text(raw, "current_password"), new)
    except ValueError as exc:
        return _fail(exc, "password change")
    return ok(scrub_secrets({"ok": True, "liveEnabled": False, "mode": "paper"}))


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
