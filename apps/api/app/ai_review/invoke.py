"""One administrator click, one checked note.

GET handlers do not call this. A click builds facts from the stored row, then
sends them only when the relay is ready. The same stored facts are not sent
twice while the first result is still remembered. A failed send is remembered
too, so the repeat does not call again and does not count again.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from typing import Any, Callable, Optional

from app.ai_review.model_adapter import facts_for_model, validate_model_output
from app.ai_review.relay import RelayDisabled, RelayError, public_status, ready
from app.ai_review.schema import STRATEGY_ID, SYMBOL

_TTL = 120.0
_lock = threading.Lock()
_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_flight: dict[str, threading.Event] = {}
_box: dict[str, dict[str, Any]] = {}

ClientFactory = Callable[[], Any]


def clear_clicks() -> None:
    with _lock:
        _cache.clear()
        _flight.clear()
        _box.clear()


def network_client():
    from app.ai_review.relay import RelayClient

    return RelayClient()


def interpret_request(body: Any, client_factory: Optional[ClientFactory] = None) -> dict[str, Any]:
    problem = _problem(body)
    if problem:
        return _closed(problem)
    if body["kind"] == "signal":
        target = _signal_target(str(body["strategy"]), str(body["symbol"]), int(body["asof"]))
    else:
        target = _decision_target(str(body["id"]))
    if target.get("error"):
        return _closed(str(target["error"]), sample=target.get("sample"))
    if not target.get("callable"):
        return _quiet(target, reason=str(target.get("reason") or "not_sent"))
    if not ready():
        state = public_status()
        return _quiet(target, reason=str(state.get("reason") or state["status"]))
    facts = target["facts"]
    key = hashlib.sha256(
        json.dumps({"kind": body["kind"], "facts": facts}, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()
    result = _once(key, facts, client_factory or network_client)
    return _pack(target, result)


def _signal_target(strategy: str, symbol: str, asof: int) -> dict[str, Any]:
    from app.ai_review.service import explain_payload

    body = explain_payload(strategy, symbol, asof, model_client=None)
    if body is None:
        return {"error": "not_found"}
    rules = body["rules"]
    target = {"kind": "signal", "sample": body.get("sample"), "rules": rules, "event": None}
    if rules.get("reconstructed") != "yes":
        target["reason"] = "no_rule_evidence"
        target["callable"] = False
        return target
    target["facts"] = facts_for_model(rules)
    target["callable"] = True
    return target


def _decision_target(event_id: str) -> dict[str, Any]:
    from app.ai_review.service import decisions_payload

    payload = decisions_payload()
    rows = payload.get("events") or payload.get("decisions") or []
    event = next((row for row in rows if row.get("id") == event_id), None)
    if event is None:
        return {"error": "not_found", "sample": payload.get("sample")}
    target = {"kind": "decision", "sample": payload.get("sample"), "rules": None, "event": _public_event(event)}
    if event.get("basis") == "truncated" or event.get("attribution") == "truncated":
        target["reason"] = "truncated"
        target["callable"] = False
        return target
    target["facts"] = {
        "strategy_id": event.get("strategy_id"),
        "symbol": event.get("symbol"),
        "ts": event.get("ts"),
        "book": event.get("book"),
        "kind": event.get("kind"),
        "side": event.get("side"),
        "price": event.get("price"),
        "cost": event.get("cost"),
        "net": event.get("net"),
        "live_order": False,
        "attribution": event.get("attribution") or "stored_row",
    }
    target["callable"] = True
    return target


def _public_event(event: dict[str, Any]) -> dict[str, Any]:
    keep = ("id", "ts", "book", "strategy_id", "kind", "symbol", "side", "price", "cost", "net", "basis", "attribution", "live_order", "open")
    return {key: event.get(key) for key in keep}


def _once(key: str, facts: dict[str, Any], factory: ClientFactory) -> dict[str, Any]:
    now = time.monotonic()
    with _lock:
        cached = _cache.get(key)
        if cached is not None and now - cached[0] <= _TTL:
            found = dict(cached[1])
            found["deduped"] = True
            found["charged_now"] = False
            return found
        event = _flight.get(key)
        if event is None:
            event = threading.Event()
            _flight[key] = event
            owner = True
        else:
            owner = False
    if not owner:
        event.wait(_TTL)
        with _lock:
            found = dict(_box.get(key) or {})
        found["deduped"] = True
        found["charged_now"] = False
        return found
    result = _fail("client_error", remember=False)
    try:
        before = int(public_status()["budget_used"])
        result = _send(facts, factory)
        result["charged_now"] = int(public_status()["budget_used"]) > before
        result["deduped"] = False
        with _lock:
            if result.get("remember"):
                _cache[key] = (time.monotonic(), dict(result))
            _box[key] = dict(result)
            _flight.pop(key, None)
    finally:
        event.set()
    return result


def _send(facts: dict[str, Any], factory: ClientFactory) -> dict[str, Any]:
    try:
        raw = factory().complete(facts, timeout_s=float(public_status()["timeout_sec"]))
    except RelayDisabled as exc:
        return _fail(exc.reason, remember=False)
    except RelayError as exc:
        return _fail(exc.reason, remember=exc.reason not in {"busy", "relay_off"})
    except TimeoutError:
        return _fail("timeout", remember=True)
    except Exception:
        return _fail("client_error", remember=True)
    checked = validate_model_output(raw, facts)
    if checked is None:
        return _fail("unverified", remember=True)
    return {
        "status": "accepted",
        "reason": None,
        "text": checked["summary"],
        "citations": checked["citations"],
        "sent": True,
        "remember": True,
    }


def _fail(reason: str, *, remember: bool) -> dict[str, Any]:
    return {"status": "failed", "reason": reason, "text": None, "citations": [], "sent": reason not in {"busy", "relay_off"}, "remember": remember}


def _pack(target: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    state = public_status()
    sent = bool(result.get("sent")) and not result.get("deduped")
    return {
        "ok": True,
        "kind": target["kind"],
        "sample": bool(target.get("sample")),
        "sent": sent,
        "called": sent,
        "deduped": bool(result.get("deduped")),
        "charged_now": bool(result.get("charged_now")),
        "connected": False,
        "orders_enabled": False,
        "live_enabled": False,
        "not_a_return": True,
        "not_an_order": True,
        "profit_promise": False,
        "rules": target.get("rules"),
        "event": target.get("event"),
        "ai": {
            "status": result.get("status"),
            "reason": result.get("reason"),
            "text": result.get("text"),
            "citations": result.get("citations") or [],
            "live": False,
            "external": False,
            "mock": False,
        },
        "budget": {
            "unit": state["budget_unit"],
            "used": state["budget_used"],
            "limit": state["budget_limit"],
            "day": state["budget_day"],
            "currency": None,
            "guaranteed": False,
        },
    }


def _quiet(target: dict[str, Any], reason: str) -> dict[str, Any]:
    packed = _pack(target, {"status": "not_sent", "reason": reason, "text": None, "citations": [], "sent": False, "charged_now": False, "deduped": False})
    packed["ok"] = True
    return packed


def _closed(reason: str, sample: Any = None) -> dict[str, Any]:
    return {
        "ok": False,
        "reason": reason,
        "sample": sample,
        "sent": False,
        "called": False,
        "charged_now": False,
        "connected": False,
        "orders_enabled": False,
        "live_enabled": False,
        "not_a_return": True,
        "not_an_order": True,
    }


def _problem(body: Any) -> Optional[str]:
    if not isinstance(body, dict):
        return "bad_body"
    kind = body.get("kind")
    if kind == "signal":
        if set(body) != {"kind", "strategy", "symbol", "asof"}:
            return "bad_body"
        if not isinstance(body.get("strategy"), str) or STRATEGY_ID.fullmatch(body["strategy"]) is None:
            return "bad_strategy"
        if not isinstance(body.get("symbol"), str) or SYMBOL.fullmatch(body["symbol"]) is None:
            return "bad_symbol"
        asof = body.get("asof")
        if isinstance(asof, bool) or not isinstance(asof, int) or asof <= 0 or asof > 10**15:
            return "bad_time"
        return None
    if kind == "decision":
        if set(body) != {"kind", "id"}:
            return "bad_body"
        event_id = body.get("id")
        if not isinstance(event_id, str) or not 1 <= len(event_id) <= 160:
            return "bad_id"
        if any(ch not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_:.-" for ch in event_id):
            return "bad_id"
        return None
    return "bad_body"
