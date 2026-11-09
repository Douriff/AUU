"""Read-only spot screener.

POST carries a query but writes nothing: no orders, no ledger, no live switch.
Login is the existing auth gate. This path is not public, so an anonymous caller
cannot read the screened book.
"""
from __future__ import annotations

import json
import threading

from fastapi import APIRouter, Request
from fastapi.responses import Response

from app.routes.envelope import err, ok
from app.screener.logic import LIVE_ENABLED, SchemaError
from app.screener.market import ScreenerBook, build_default_book

router = APIRouter(prefix="/api/v1/screener", tags=["screener"])

_MAX_BODY = 8000
_LOCK = threading.Lock()
_BOOK: ScreenerBook | None = None


def get_book() -> ScreenerBook:
    global _BOOK
    with _LOCK:
        if _BOOK is None:
            _BOOK = build_default_book()
        return _BOOK


def set_book(book: ScreenerBook | None) -> None:
    global _BOOK
    with _LOCK:
        _BOOK = book


def _bad():
    return err("BAD_REQUEST", "筛币条件无效或超出限制", 400)


async def _body(request: Request):
    raw = await request.body()
    if len(raw) > _MAX_BODY:
        return None, _bad()
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, _bad()
    return payload, None


@router.get("/spot")
def spot_meta():
    """Schema, universe definition and candle rules. No prices."""
    body = get_book().meta()
    body["liveEnabled"] = LIVE_ENABLED
    return ok(body)


@router.post("/spot")
async def spot_screen(request: Request):
    payload, problem = await _body(request)
    if problem is not None:
        return problem
    try:
        data = get_book().screen(payload)
    except SchemaError:
        return _bad()
    data["liveEnabled"] = LIVE_ENABLED
    return ok(data)


@router.post("/spot/csv")
async def spot_csv(request: Request):
    payload, problem = await _body(request)
    if problem is not None:
        return problem
    try:
        text = get_book().csv_text(payload)
    except SchemaError:
        return _bad()
    return Response(
        content=text,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="auu-spot-screener.csv"',
            "Cache-Control": "no-store",
            "X-Api-Version": "1",
        },
    )
