"""AI section HTTP surface: catalog, matcher, system runner, personal studio.

Compile is a catalog matcher, not a strategy compiler. Studio drafts and
paper agents are per-user, owner-only, and never write the site-wide M3
runner. Interpret / relay stay on the existing admin-gated ai-review
routes and are not re-exported. JSON body cap (8000 bytes) and
BAD_REQUEST / bad_body follow existing conventions.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from collections import deque

from fastapi import APIRouter, Request

from app.ai_section.catalog_view import catalog_payload
from app.ai_section.body import read_bounded_json
from app.ai_section.compile import MAX_PROMPT, match_catalog
from app.ai_section.runner_view import public_runner_status
from app.ai_section.schema import NAME_RE, SchemaError, kinds_payload, validate_cost_override, validate_strategy_body
from app.ai_section.simulate import run_simulation
from app.ai_section.store import StoreError, get_store
from app.routes.envelope import err, ok

log = logging.getLogger("auu.ai_section")
router = APIRouter(prefix="/api/v1/ai-section", tags=["ai-section"])

class _Rate:
    def __init__(self, limit: int, window: float):
        self.limit, self.window = limit, window
        self._lock = threading.Lock()
        self._hits: dict[str, deque] = {}

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            q = self._hits.setdefault(key, deque())
            while q and now - q[0] > self.window:
                q.popleft()
            if len(q) >= self.limit:
                return False
            q.append(now)
            return True


WRITE_LIMIT = _Rate(30, 60.0)
READ_LIMIT = _Rate(240, 60.0)
SIM_LIMIT = _Rate(10, 60.0)


def _finish(request: Request, response):
    response.headers["Cache-Control"] = "private, no-store"
    log.info("ai_section %s %s", request.method, request.url.path)
    return response


async def _json_body(request: Request):
    result = await read_bounded_json(request)
    if result.status == "too_large":
        return {"_too_large": True, "_consumed": result.bytes_consumed, "_stopped": result.stopped_early}
    if result.status == "bad":
        return {"_bad": True, "_consumed": result.bytes_consumed}
    return result.data or {}


def _body_error(body):
    if not isinstance(body, dict):
        return err("BAD_REQUEST", "bad_body", 400)
    if body.get("_too_large"):
        return err("TOO_LARGE", "request body too large", 413)
    if body.get("_bad"):
        return err("BAD_REQUEST", "bad_body", 400)
    return None


def _uid(request: Request):
    from app.routes.mainstream_paper import _uid as existing_uid

    return existing_uid(request)


def _need_login():
    return err("AUTH_REQUIRED", "请先登录", 401)


def _gate(request: Request, limiter: _Rate):
    uid = _uid(request)
    if uid is None:
        return None, _need_login()
    if not limiter.allow(uid):
        return None, err("RATE_LIMITED", "too many requests", 429, {"retryAfterSec": int(limiter.window)})
    return uid, None


def _store_err(exc: StoreError | SchemaError):
    status = getattr(exc, "status", 400)
    return err(exc.code, exc.message, status)


@router.get("")
def section_meta(request: Request):
    body = {
        "paper_only": True,
        "live_enabled": False,
        "auto_model": False,
        "used_model": False,
        "returns_published": False,
        "studio_connected": True,
        "source_studio_ported": False,
        "agents_connected": True,
        "personal_paper_agents": True,
        "source_agents_ported": False,
        "copy_trading": False,
        "endpoints": {
            "catalog": "GET /api/v1/ai-section/catalog",
            "compile": "POST /api/v1/ai-section/compile (catalog match only, not a compiler)",
            "runner": "GET /api/v1/ai-section/runner (login, trimmed site-wide paper status)",
            "kinds": "GET /api/v1/ai-section/studio/kinds",
            "strategies": "GET/POST /api/v1/ai-section/strategies (login, owner only)",
            "agents": "GET/POST /api/v1/ai-section/agents (login, owner only, personal paper)",
        },
        "reuse": {
            "interpret": "POST /api/v1/ai-review/interpret (existing admin relay, explicit click only)",
        },
    }
    return _finish(request, ok(body))


@router.get("/catalog")
def catalog(request: Request):
    try:
        payload = catalog_payload()
    except Exception:
        log.info("ai_section catalog failed")
        return err("CATALOG_FAILED", "catalog unavailable", 503)
    return _finish(request, ok(payload))


@router.get("/runner")
def runner(request: Request):
    if _uid(request) is None:
        return _need_login()
    try:
        from app.paper.strategy_runner import get_runner

        payload = public_runner_status(get_runner().summary())
    except Exception:
        log.info("ai_section runner failed")
        return err("RUNNER_FAILED", "runner unavailable", 503)
    return _finish(request, ok(payload))


@router.post("/compile")
async def compile_route(request: Request):
    body = await _json_body(request)
    denied_body = _body_error(body)
    if denied_body is not None:
        return denied_body
    if body.get("model") or body.get("invoke") or body.get("live"):
        return err("BAD_REQUEST", "bad_body", 400)
    prompt = body.get("prompt")
    if not isinstance(prompt, str) or len(prompt) > MAX_PROMPT:
        return err("BAD_REQUEST", "bad_body", 400)
    result = match_catalog(prompt)
    if not result.get("ok"):
        return err(str(result.get("code") or "BAD_REQUEST"), "bad_body", 400)
    data = {key: value for key, value in result.items() if key not in {"ok", "code", "message"}}
    return _finish(request, ok(data))


@router.get("/studio/kinds")
def studio_kinds(request: Request):
    return _finish(request, ok({"kinds": kinds_payload(), "generates_code": False, "llm": False}))


@router.get("/strategies")
def list_strategies(request: Request):
    uid, denied = _gate(request, READ_LIMIT)
    if denied is not None:
        return denied
    return _finish(request, ok({"strategies": get_store().list_strategies(uid), "scope": "owner"}))


@router.post("/strategies")
async def create_strategy(request: Request):
    uid, denied = _gate(request, WRITE_LIMIT)
    if denied is not None:
        return denied
    body = await _json_body(request)
    denied_body = _body_error(body)
    if denied_body is not None:
        return denied_body
    try:
        draft = validate_strategy_body(body)
        return _finish(request, ok(get_store().create_strategy(uid, draft)))
    except (SchemaError, StoreError) as exc:
        return _store_err(exc)


@router.get("/strategies/{sid}")
def get_strategy(request: Request, sid: str):
    uid, denied = _gate(request, READ_LIMIT)
    if denied is not None:
        return denied
    if not re.fullmatch(r"^s_[0-9a-f]{16}$", sid):
        return err("NOT_FOUND", "not found", 404)
    try:
        return _finish(request, ok(get_store().get_strategy(uid, sid)))
    except StoreError as exc:
        return _store_err(exc)


@router.patch("/strategies/{sid}")
async def patch_strategy(request: Request, sid: str):
    uid, denied = _gate(request, WRITE_LIMIT)
    if denied is not None:
        return denied
    if not re.fullmatch(r"^s_[0-9a-f]{16}$", sid):
        return err("NOT_FOUND", "not found", 404)
    body = await _json_body(request)
    denied_body = _body_error(body)
    if denied_body is not None:
        return denied_body
    try:
        draft = validate_strategy_body(body)
        return _finish(request, ok(get_store().update_strategy(uid, sid, draft)))
    except (SchemaError, StoreError) as exc:
        return _store_err(exc)


@router.delete("/strategies/{sid}")
def delete_strategy(request: Request, sid: str):
    uid, denied = _gate(request, WRITE_LIMIT)
    if denied is not None:
        return denied
    if not re.fullmatch(r"^s_[0-9a-f]{16}$", sid):
        return err("NOT_FOUND", "not found", 404)
    try:
        return _finish(request, ok(get_store().delete_strategy(uid, sid)))
    except StoreError as exc:
        return _store_err(exc)


@router.get("/agents")
def list_agents(request: Request):
    uid, denied = _gate(request, READ_LIMIT)
    if denied is not None:
        return denied
    return _finish(request, ok({
        "agents": get_store().list_agents(uid),
        "scope": "owner",
        "personal": True,
        "site_runner": False,
        "live": False,
    }))


@router.post("/agents")
async def create_agent(request: Request):
    uid, denied = _gate(request, WRITE_LIMIT)
    if denied is not None:
        return denied
    body = await _json_body(request)
    denied_body = _body_error(body)
    if denied_body is not None:
        return denied_body
    name = str(body.get("name") or "").strip()
    strategy_id = str(body.get("strategy_id") or "")
    if not NAME_RE.fullmatch(name) or not re.fullmatch(r"^s_[0-9a-f]{16}$", strategy_id):
        return err("BAD_REQUEST", "bad_body", 400)
    if set(body) - {"name", "strategy_id"}:
        return err("BAD_FIELD", "bad_body", 400)
    try:
        return _finish(request, ok(get_store().create_agent(uid, name, strategy_id)))
    except StoreError as exc:
        return _store_err(exc)


@router.get("/agents/{aid}")
def get_agent(request: Request, aid: str):
    uid, denied = _gate(request, READ_LIMIT)
    if denied is not None:
        return denied
    if not re.fullmatch(r"^a_[0-9a-f]{16}$", aid):
        return err("NOT_FOUND", "not found", 404)
    try:
        row = get_store().get_agent(uid, aid)
        row.update({"personal": True, "site_runner": False, "live": False})
        return _finish(request, ok(row))
    except StoreError as exc:
        return _store_err(exc)


@router.delete("/agents/{aid}")
def delete_agent(request: Request, aid: str):
    uid, denied = _gate(request, WRITE_LIMIT)
    if denied is not None:
        return denied
    if not re.fullmatch(r"^a_[0-9a-f]{16}$", aid):
        return err("NOT_FOUND", "not found", 404)
    try:
        return _finish(request, ok(get_store().delete_agent(uid, aid)))
    except StoreError as exc:
        return _store_err(exc)


@router.get("/agents/{aid}/simulations")
def list_simulations(request: Request, aid: str):
    uid, denied = _gate(request, READ_LIMIT)
    if denied is not None:
        return denied
    if not re.fullmatch(r"^a_[0-9a-f]{16}$", aid):
        return err("NOT_FOUND", "not found", 404)
    try:
        return _finish(request, ok({"simulations": get_store().list_simulations(uid, aid), "personal": True}))
    except StoreError as exc:
        return _store_err(exc)


@router.post("/agents/{aid}/simulate")
async def simulate_agent(request: Request, aid: str):
    uid, denied = _gate(request, SIM_LIMIT)
    if denied is not None:
        return denied
    if not re.fullmatch(r"^a_[0-9a-f]{16}$", aid):
        return err("NOT_FOUND", "not found", 404)
    body = await _json_body(request)
    denied_body = _body_error(body)
    if denied_body is not None:
        return denied_body
    if set(body) - {"fee_bps", "slippage_bps"}:
        return err("BAD_REQUEST", "bad_body", 400)
    try:
        store = get_store()
        agent = store.get_agent(uid, aid)
        strategy = store.get_strategy(uid, agent["strategy_id"])
        costs = validate_cost_override(body, strategy.get("risk") or {})
        result = run_simulation(strategy, fee_bps=costs["fee_bps"], slippage_bps=costs["slippage_bps"])
        record = store.add_simulation(uid, aid, strategy["id"], result["status"], result.get("request") or {}, result)
        return _finish(request, ok(record))
    except (SchemaError, StoreError) as exc:
        return _store_err(exc)


@router.api_route("/agents/{aid}/start", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
@router.api_route("/agents/{aid}/stop", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
@router.api_route("/agents/{aid}/orders", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
def refuse_live_agent(request: Request, aid: str = ""):
    return err("METHOD_NOT_ALLOWED", "personal paper agents do not start the site runner, live trading, or orders", 405)


@router.api_route("/interpret", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
def refuse_interpret_alias(request: Request):
    return err("METHOD_NOT_ALLOWED", "use existing /api/v1/ai-review routes; this section does not proxy them", 405)


@router.api_route("/relay", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
def refuse_relay_alias(request: Request):
    return err("METHOD_NOT_ALLOWED", "use existing /api/v1/ai-review routes; this section does not proxy them", 405)


@router.api_route("", methods=["PUT", "PATCH", "DELETE"])
def reject_meta_writes(request: Request):
    return err("METHOD_NOT_ALLOWED", "ai section is paper and read-only at the root", 405)
