"""Read-only spot screener.

POST carries a query but writes nothing: no orders, no ledger, no live switch.
The shared login gate still rejects anonymous callers with 401. This router then
allows only an administrator session. Fixture data uses the same check.
"""
from __future__ import annotations

import json
import threading

from fastapi import APIRouter, Request
from fastapi.responses import Response

from app.auth.accounts import auth_enabled
from app.routes.auth import session_user
from app.routes.envelope import err, ok
from app.screener.access import decide
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


def _guard(request: Request):
    """Refuse before any book read. Headers and fixture mode are not a grant."""
    verdict = decide(session_user(request), auth_on=auth_enabled())
    if verdict is None:
        return None
    status, code, message = verdict
    return err(code, message, status)


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
def spot_meta(request: Request):
    """Schema, universe definition and candle rules. No prices."""
    refused = _guard(request)
    if refused is not None:
        return refused
    body = get_book().meta()
    body["liveEnabled"] = LIVE_ENABLED
    return ok(body)


@router.post("/spot")
async def spot_screen(request: Request):
    refused = _guard(request)
    if refused is not None:
        return refused
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
    refused = _guard(request)
    if refused is not None:
        return refused
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
