"""行业动态 API: paginated headlines from news.sqlite (login required; reads the local store only, never upstream)."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Query, Request

from app.routes.envelope import err, ok
from app.routes.mainstream_paper import _uid

router = APIRouter(prefix="/api/v1/news", tags=["news"])

NOTE = "标题与摘要来自各媒体公开 RSS，版权归原作者；点击标题阅读原文。仅供参考，不构成投资建议。"


@router.get("")
def news(
    request: Request,
    page: int = Query(1, ge=1, le=1000),
    size: int = Query(20, ge=1, le=50),
    coin: Optional[str] = Query(None, max_length=8),
    focus: bool = False,
    important: bool = False,
    source: Optional[str] = Query(None, max_length=32),
):
    if _uid(request) is None:  # the auth gate answers first; checked again so the route never leaks without it
        return err("AUTH_REQUIRED", "请先登录", 401)
    from app.news import COINS, SOURCES, enabled_sources, interval_sec, news_enabled, peek_store

    c = (coin or "").strip().upper() or None
    if c is not None and c not in COINS:
        return err("BAD_COIN", "不支持的币种", 400)
    s = (source or "").strip().lower() or None
    if s is not None and s not in SOURCES:
        return err("BAD_SOURCE", "不支持的来源", 400)
    meta = {"enabled": news_enabled(), "intervalSec": interval_sec(), "coins": COINS, "note": NOTE}
    try:
        st = peek_store()
        if st is None:
            data = {"items": [], "page": page, "size": size, "total": 0, "pages": 1, "lastFetchedAt": None,
                    "sources": [{"id": k, "name": SOURCES[k]["name"], "home": SOURCES[k]["home"], "lastOkAt": None, "lastAttemptAt": None,
                                 "ok": False, "fails": 0, "error": None} for k in enabled_sources()]}
        else:
            data = st.page(page=page, size=size, coin=c, focus=focus, important=important, source=s)
    except Exception:
        return err("NEWS_UNAVAILABLE", "行业动态暂时不可用", 503)
    return ok({**data, **meta})
