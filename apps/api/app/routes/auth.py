"""Paper accounts: register, session cookie, leaderboard. No custody."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel

from app.auth.accounts import (
    account_row,
    auth_enabled,
    authenticate,
    invite_code,
    issue_token,
    journal_for,
    leaderboard,
    list_users,
    register,
    signup_allowed,
    user_by_id,
    user_from_token,
)
from app.paper.books import pop_book, push_book
from app.routes.envelope import err, ok

router = APIRouter(prefix="/api/v1", tags=["auth"])

COOKIE = "auu_session"
_MESSAGES = {
    "AUTH_OFF": "登录未开启",
    "SIGNUP_CLOSED": "注册已关闭",
    "INVITE": "邀请码不正确",
    "BAD_NAME": "用户名需为 2–24 位字母、数字或中文",
    "BAD_PASSWORD": "密码至少 8 位",
    "BAD_START": "起始 SOL 需在 1 到 100000",
    "NAME_TAKEN": "用户名已存在",
}


class AuthBody(BaseModel):
    name: str = ""
    password: str = ""
    invite: str = ""
    start_sol: Optional[float] = None


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
def auth_register(body: AuthBody):
    try:
        user = register(body.name, body.password, body.invite, body.start_sol)
    except ValueError as exc:
        code = str(exc)
        return err(code, _MESSAGES.get(code, code), 400)
    return _stamp(ok(_me_payload(user_by_id(user["id"]))), user["id"])


@router.post("/auth/login")
def auth_login(body: AuthBody):
    if not auth_enabled():
        return err("AUTH_OFF", _MESSAGES["AUTH_OFF"], 400)
    user = authenticate(body.name, body.password)
    if user is None:
        return err("BAD_LOGIN", "用户名或密码错误", 401)
    return _stamp(ok(_me_payload(user)), user["id"])


@router.post("/auth/logout")
def auth_logout():
    response = ok({"ok": True, "liveEnabled": False})
    response.delete_cookie(COOKIE, path="/")
    return response


@router.get("/auth/me")
def auth_me(request: Request):
    return ok(_me_payload(session_user(request)))


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
        {
            "mode": "paper",
            "liveEnabled": False,
            "items": [account_row(user_by_id(row["id"])) for row in list_users() if user_by_id(row["id"])],
        }
    )


@router.get("/leaderboard")
def auth_leaderboard(sort: str = Query("pnl")):
    key = "return" if sort in {"return", "return_pct"} else "pnl"
    return ok(leaderboard(key))
