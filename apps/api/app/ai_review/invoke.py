"""One administrator click, one upstream attempt.

GET handlers do not call this. A click builds facts on the server, checks the
pinned host, then reserves one persistent attempt before the socket is opened.
The same fact hash is never dispatched again. A failed send stays counted.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Callable, Optional

from app.ai_review.dispatch_quota import mark, release_unsent, reserve
from app.ai_review.model_adapter import classify_model_output, facts_for_model
from app.ai_review.relay import (
    ALLOWED_HOST,
    RelayDisabled,
    RelayError,
    end_begin,
    public_addresses,
    public_status,
    ready,
    set_pinned_address,
    try_begin,
)
from app.ai_review.schema import STRATEGY_ID, SYMBOL

ClientFactory = Callable[[], Any]
_RELEASE = {"not_reserved", "secret_in_body", "host_not_allowed", "name_lookup_failed", "busy", "relay_off"}


def clear_clicks() -> None:
    return None


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
    if not try_begin():
        return _fail("busy", sent=False)
    try:
        try:
            address = public_addresses(ALLOWED_HOST)[0]
        except RelayError as exc:
            return _fail(exc.reason, sent=False)
        state = public_status()
        if state.get("budget_mode") == "invalid":
            return _fail("budget_invalid", sent=False)
        if state.get("budget_mode") == "unlimited":
            claimed = reserve(key, str(state["budget_day"]), None)
        elif state.get("budget_mode") == "count" and isinstance(state.get("budget_limit"), int):
            claimed = reserve(key, str(state["budget_day"]), int(state["budget_limit"]))
        else:
            return _fail("budget_invalid", sent=False)
        if not claimed["ok"]:
            if claimed["reason"] == "duplicate":
                return {**_fail("already_dispatched", sent=False), "deduped": True, "charged_now": False}
            return {**_fail("budget", sent=False), "deduped": False, "charged_now": False}
        set_pinned_address(address)
        try:
            result = _send(key, facts, factory)
        finally:
            set_pinned_address(None)
        if result.get("release"):
            release_unsent(key)
            result["charged_now"] = False
        else:
            if result.get("mark"):
                mark(key, str(result["mark"]))
            result["charged_now"] = True
        result["deduped"] = False
        return result
    finally:
        end_begin()


def _send(key: str, facts: dict[str, Any], factory: ClientFactory) -> dict[str, Any]:
    try:
        raw = factory().complete(facts, timeout_s=float(public_status()["timeout_sec"]), reservation=key)
    except RelayDisabled:
        return {**_fail("relay_off", sent=False), "release": True}
    except RelayError as exc:
        if exc.reason in _RELEASE:
            return {**_fail(exc.reason, sent=False), "release": True}
        return {**_fail(exc.reason, sent=True), "release": False}
    except TimeoutError:
        return {**_fail("timeout", sent=True), "release": False}
    except OSError:
        return {**_fail("connection_error", sent=True), "release": False}
    except Exception:
        return {**_fail("client_error", sent=True), "release": False}
    checked, reason = classify_model_output(raw, facts)
    if checked is None:
        return {**_fail(reason or "unverified", sent=True), "release": False, "mark": reason or "unverified"}
    return {
        "status": "accepted",
        "reason": None,
        "text": checked["summary"],
        "citations": checked["citations"],
        "sent": True,
        "release": False,
        "mark": "accepted",
    }


def _fail(reason: str, *, sent: bool) -> dict[str, Any]:
    quiet = reason in {"busy", "budget", "budget_invalid", "already_dispatched", "relay_off", "host_not_allowed", "name_lookup_failed"}
    return {
        "status": "not_sent" if quiet else "failed",
        "reason": reason,
        "text": None,
        "citations": [],
        "sent": sent,
    }


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
            "mode": state["budget_mode"],
            "unlimited": state["budget_unlimited"],
            "day": state["budget_day"],
            "currency": None,
            "guaranteed": False,
            "price_known": False,
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
