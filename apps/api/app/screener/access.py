"""Who may read the spot screener.

The website's subscription toggle is display-only (``subscription.ts``). The points
catalog's only item is the cosmetic ``example_badge``, and nothing in the app treats
it as a market entitlement. There is no trusted subscription record to honor, so this
preview does not invent one, does not read client headers, and does not read that badge.

With accounts on, only an administrator session may preview. A signed-in account that
is not an administrator is refused. With accounts off there is no administrator
session, so the preview is refused too. The refusal body has no quotes.
"""
from __future__ import annotations

from typing import Any, Optional

CODE = "SCREENER_ADMIN"
MESSAGE = "筛币仍是管理员预览。服务端还没有行情订阅记录，普通账号不能读取这些行情。"


def decide(user: Optional[dict[str, Any]], *, auth_on: bool) -> Optional[tuple[int, str, str]]:
    """Return ``(status, code, message)`` to refuse, or None to allow.

    ``user`` must be the account loaded from the session cookie. Extra keys such as
    ``subscribed`` are ignored. ``is_admin`` is the only grant.
    """
    if not auth_on or user is None:
        if user is None and auth_on:
            return (401, "AUTH_REQUIRED", "请先登录")
        return (403, CODE, MESSAGE)
    if not user.get("is_admin"):
        return (403, CODE, MESSAGE)
    return None
