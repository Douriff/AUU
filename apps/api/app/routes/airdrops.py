"""空投雷达 API: curated + RSS-ingested list/detail (login required; local store only)."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Query, Request

from app.routes.envelope import err, ok
from app.routes.mainstream_paper import _uid

router = APIRouter(prefix="/api/v1/airdrops", tags=["airdrops"])

NOTE = (
    "空投、积分与模因生态相关资讯来自公开 RSS 关键词筛选与编辑整理，每条均标注来源；"
    "点击可查看站内摘要并跳转原文。仅供参考，不构成投资、参与或收益建议；请自行核实官方渠道，警惕钓鱼链接。"
)


@router.get("")
def airdrops_list(
    request: Request,
    category: str = Query("all", max_length=16),
    status: Optional[str] = Query(None, max_length=16),
    origin: Optional[str] = Query(None, max_length=16),
    page: int = Query(1, ge=1, le=1000),
    size: int = Query(20, ge=1, le=50),
):
    if _uid(request) is None:
        return err("AUTH_REQUIRED", "请先登录", 401)
    from app.airdrops import CATEGORIES, STATUSES, airdrops_enabled, peek_store
    from app.airdrops.ingest import FEED_SOURCES, enabled_feeds, ingest_enabled, interval_sec

    cat = (category or "all").strip().lower()
    if cat not in {"all", *CATEGORIES}:
        return err("BAD_CATEGORY", "不支持的分类", 400)
    st = (status or "").strip().lower() or None
    if st is not None and st not in STATUSES:
        return err("BAD_STATUS", "不支持的状态", 400)
    org = (origin or "").strip().lower() or None
    if org is not None and org not in {"curated", "ingested"}:
        return err("BAD_ORIGIN", "不支持的来源类型", 400)
    meta = {
        "enabled": airdrops_enabled(),
        "ingestEnabled": ingest_enabled(),
        "ingestIntervalSec": interval_sec(),
        "categories": list(CATEGORIES),
        "statuses": list(STATUSES),
        "note": NOTE,
    }
    try:
        store = peek_store()
        if store is None:
            data = {
                "items": [],
                "page": page,
                "size": size,
                "total": 0,
                "pages": 1,
                "seededAt": None,
                "lastIngestAt": None,
                "feeds": [
                    {"id": k, "name": FEED_SOURCES[k]["name"], "home": FEED_SOURCES[k]["home"],
                     "lastOkAt": None, "lastAttemptAt": None, "ok": False, "fails": 0, "error": None}
                    for k in enabled_feeds()
                ],
            }
        else:
            data = store.list(category=cat, status=st, origin=org, page=page, size=size)
    except Exception:
        return err("AIRDROPS_UNAVAILABLE", "空投雷达暂时不可用", 503)
    return ok({**data, **meta})


@router.get("/{item_id}")
def airdrop_detail(request: Request, item_id: str):
    if _uid(request) is None:
        return err("AUTH_REQUIRED", "请先登录", 401)
    from app.airdrops import airdrops_enabled, peek_store

    iid = (item_id or "").strip()[:80]
    if not iid:
        return err("NOT_FOUND", "未找到该空投资讯", 404)
    try:
        store = peek_store()
        item = store.get(iid) if store is not None else None
    except Exception:
        return err("AIRDROPS_UNAVAILABLE", "空投雷达暂时不可用", 503)
    if item is None:
        return err("NOT_FOUND", "未找到该空投资讯", 404)
    return ok({"item": item, "enabled": airdrops_enabled(), "note": NOTE})
