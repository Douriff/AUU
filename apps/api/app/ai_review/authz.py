"""Trusted-session admin gate for the new lab only.

Anonymous -> 401. Signed-in non-admin -> 403 with no strategy payload.
Accounts disabled -> 401 even if a cookie or client header is presented.
Forged cookies and local flags are not consulted.
"""
from __future__ import annotations

from fastapi import Request

from app.auth.accounts import auth_enabled, user_from_token
from app.auth.gate import COOKIE
from app.routes.envelope import err


def require_admin(request: Request):
    """Return an error response, or None when the caller is a real admin."""
    if not auth_enabled():
        return err("AUTH_DISABLED", "账户系统未开启，策略复盘不开放。", 401)
    try:
        user = user_from_token(request.cookies.get(COOKIE) or "")
    except Exception:
        user = None
    if not user:
        return err("AUTH_REQUIRED", "请先登录。", 401)
    if user.get("is_admin") is not True:
        return err("ADMIN_REQUIRED", "只有管理员可以查看策略复盘。", 403)
    return None
