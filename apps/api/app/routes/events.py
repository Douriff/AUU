"""GET /api/v1/events — read-only console feed. Never enables live."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Query

from app.paper.events import build_events
from app.routes.envelope import ok

router = APIRouter(prefix="/api/v1", tags=["events"])


@router.get("/events")
def events(
    since: Optional[str] = Query(None),
    limit: int = Query(300, ge=1, le=1000),
):
    return ok(build_events(since=since, limit=limit))
