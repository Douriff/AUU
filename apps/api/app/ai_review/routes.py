"""Read-only HTTP surface. Admin session required on every method."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Query, Request
from fastapi.responses import Response

from app.ai_review.authz import require_admin
from app.ai_review.schema import MAX_COMPARE, MAX_LIMIT, STRATEGY_ID, SYMBOL
from app.routes.envelope import err, ok

log = logging.getLogger("auu.ai_review")
router = APIRouter(prefix="/api/v1/ai-review", tags=["ai-review"])


def _guard(request: Request):
    denied = require_admin(request)
    if denied is not None:
        log.info("ai_review denied %s", denied.status_code)
        return denied
    if request.method not in {"GET", "HEAD"}:
        return err("METHOD_NOT_ALLOWED", "策略复盘只读。", 405)
    return None


def _strategy(raw: str):
    if not STRATEGY_ID.fullmatch(raw or ""):
        return None
    return raw


def _symbol(raw: str | None):
    if raw is None or raw == "":
        return ""
    if not SYMBOL.fullmatch(raw):
        return None
    return raw


def _limit(raw: str) -> int | None:
    if not raw.isdigit():
        return None
    value = int(raw)
    if value < 1 or value > MAX_LIMIT:
        return None
    return value


def _finish(request: Request, response):
    response.headers["Cache-Control"] = "private, no-store"
    log.info("ai_review %s %s", request.method, request.url.path)
    return response


@router.get("/plaza")
def plaza(request: Request):
    denied = _guard(request)
    if denied is not None:
        return denied
    from app.ai_review.service import plaza_payload

    return _finish(request, ok(plaza_payload()))


@router.get("/plaza.csv")
def plaza_csv(request: Request):
    denied = _guard(request)
    if denied is not None:
        return denied
    from app.ai_review.service import plaza_csv as build_csv

    body = build_csv().encode("utf-8")
    response = Response(content=body, media_type="text/csv; charset=utf-8")
    response.headers["Content-Disposition"] = "attachment; filename=strategy-review.csv"
    return _finish(request, response)


@router.get("/compare")
def compare(request: Request, ids: str = Query("")):
    denied = _guard(request)
    if denied is not None:
        return denied
    parts = [part.strip() for part in ids.split(",") if part.strip()]
    if not parts or len(parts) > MAX_COMPARE or any(_strategy(part) is None for part in parts):
        return err("BAD_REQUEST", "策略编号无效。", 400)
    from app.ai_review.service import plaza_payload

    payload = plaza_payload()
    wanted = []
    for part in parts:
        if part not in wanted:
            wanted.append(part)
    known = {card["id"] for card in payload["strategies"]}
    if any(part not in known for part in wanted):
        return err("NOT_FOUND", "没有这个策略。", 404)
    wanted_set = set(wanted)
    groups = []
    for group in payload["compare"]["groups"]:
        if wanted_set <= set(group["ids"]):
            rows = [row for row in group["rows"] if row["id"] in wanted_set]
            if len(rows) >= 2:
                groups.append({**group, "ids": [item for item in group["ids"] if item in wanted_set], "rows": rows})
    refused = [row for row in payload["compare"]["refused"] if set(row["ids"]) <= wanted_set]
    visible = [card for card in payload["strategies"] if card["id"] in wanted_set]
    body = {
        "sample": payload["sample"],
        "notice": payload["notice"],
        "strategies": visible,
        "compare": {"groups": groups, "refused": refused},
    }
    return _finish(request, ok(body))


@router.get("/strategies/{strategy_id}")
def strategy(request: Request, strategy_id: str):
    denied = _guard(request)
    if denied is not None:
        return denied
    if _strategy(strategy_id) is None:
        return err("BAD_REQUEST", "策略编号无效。", 400)
    from app.ai_review.service import strategy_payload

    body = strategy_payload(strategy_id)
    if body is None:
        return err("NOT_FOUND", "没有这个策略。", 404)
    return _finish(request, ok(body))


@router.get("/signals")
def signals(request: Request, strategy: str = Query(""), symbol: str = Query(""), limit: str = Query("50")):
    denied = _guard(request)
    if denied is not None:
        return denied
    if strategy and _strategy(strategy) is None:
        return err("BAD_REQUEST", "策略编号无效。", 400)
    parsed_symbol = _symbol(symbol)
    if parsed_symbol is None:
        return err("BAD_REQUEST", "币种无效。", 400)
    parsed_limit = _limit(limit)
    if parsed_limit is None:
        return err("BAD_REQUEST", "条数无效。", 400)
    from app.ai_review.service import signals_payload

    return _finish(request, ok(signals_payload(strategy or None, parsed_symbol or None, parsed_limit)))


@router.get("/signals/explain")
def explain(request: Request, strategy: str = Query(""), symbol: str = Query(""), asof: str = Query("")):
    denied = _guard(request)
    if denied is not None:
        return denied
    if _strategy(strategy) is None or _symbol(symbol) in {None, ""}:
        return err("BAD_REQUEST", "策略或币种无效。", 400)
    if not asof.isdigit():
        return err("BAD_REQUEST", "时间无效。", 400)
    asof_ts = int(asof)
    if asof_ts > 10**15:
        return err("BAD_REQUEST", "时间无效。", 400)
    from app.ai_review.service import explain_payload

    body = explain_payload(strategy, symbol, asof_ts)
    if body is None:
        return err("NOT_FOUND", "没有这个策略。", 404)
    return _finish(request, ok(body))


@router.get("/decisions")
def decisions(request: Request, strategy: str = Query("")):
    denied = _guard(request)
    if denied is not None:
        return denied
    if strategy and _strategy(strategy) is None:
        return err("BAD_REQUEST", "策略编号无效。", 400)
    from app.ai_review.service import decisions_payload

    return _finish(request, ok(decisions_payload(strategy or None)))


@router.api_route("", methods=["POST", "PUT", "PATCH", "DELETE"])
def writes_root(request: Request):
    return _reject_write(request)


@router.api_route("/{rest:path}", methods=["POST", "PUT", "PATCH", "DELETE"])
def writes_rest(request: Request, rest: str = ""):
    return _reject_write(request)


def _reject_write(request: Request):
    denied = _guard(request)
    if denied is not None:
        return denied
    return err("METHOD_NOT_ALLOWED", "策略复盘只读。", 405)
