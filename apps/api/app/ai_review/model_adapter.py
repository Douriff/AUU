"""Optional model adapter. External calls are not started in this phase.

The production path never constructs a network client. Tests may inject a
mock. Output that contradicts the supplied facts, or that cannot be checked,
is discarded. The adapter has no tools and cannot change a rule verdict.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from typing import Any, Optional, Protocol

_NUMBER = re.compile(r"(?<![\w.])(-?\d+(?:\.\d+)?)(?![\w.])")
_INJECTION = re.compile(
    r"ignore previous|tool call|system prompt|guaranteed profit|place an order|arm live|ignore the stop",
    re.IGNORECASE,
)
_MAX_INPUT = 4000
_MAX_SUMMARY = 400
_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_CACHE_LIMIT = 64
_CACHE_TTL = 300.0


class ModelClient(Protocol):
    is_mock: bool

    def complete(self, facts: dict[str, Any], timeout_s: float) -> Any: ...


def external_calls_enabled() -> bool:
    """The relay module decides when a call is allowed. This flag is no longer a hard stop."""
    from app.ai_review.relay import ready

    return ready()


def model_gate() -> dict[str, Any]:
    from app.ai_review.relay import public_status

    relay = public_status()
    return {
        "status": relay["status"],
        "live": False,
        "external": False,
        "invoked": relay["invoked"],
        "text": None,
        "reason": relay.get("reason"),
        "mock": False,
        "relay": relay,
    }


def network_client():
    from app.ai_review.relay import RelayClient

    return RelayClient()


def clear_cache() -> None:
    _CACHE.clear()


def _flatten(value: Any, prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    if isinstance(value, dict):
        for key, item in value.items():
            child = f"{prefix}.{key}" if prefix else str(key)
            out.update(_flatten(item, child))
        return out
    if isinstance(value, list):
        for index, item in enumerate(value):
            out.update(_flatten(item, f"{prefix}.{index}"))
        return out
    if prefix:
        out[prefix] = value
    return out


def validate_model_output(raw: Any, facts: dict[str, Any]) -> Optional[dict[str, Any]]:
    if not isinstance(raw, dict) or set(raw) != {"summary", "citations"}:
        return None
    summary = raw.get("summary")
    citations = raw.get("citations")
    if not isinstance(summary, str):
        return None
    text = summary.strip()
    if not text or len(text) > _MAX_SUMMARY:
        return None
    if _INJECTION.search(text) or not _numbers_allowed(text, facts):
        return None
    if any(ord(ch) < 32 and ch != "\n" for ch in text):
        return None
    if not isinstance(citations, list) or not citations or len(citations) > 12:
        return None
    flat = _flatten(facts)
    checked = []
    for item in citations:
        if not isinstance(item, dict) or set(item) != {"key", "value"}:
            return None
        key = item.get("key")
        if not isinstance(key, str) or key not in flat or flat[key] != item.get("value"):
            return None
        checked.append({"key": key, "value": item["value"]})
    return {"summary": text, "citations": checked}


def _fact_numbers(facts: dict[str, Any]) -> list[float]:
    found = []
    for value in _flatten(facts).values():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        number = float(value)
        if number == number and number not in {float("inf"), float("-inf")}:
            found.append(number)
    return found


def _close(left: float, right: float) -> bool:
    return abs(left - right) <= 1e-6 * max(1.0, abs(right))


def _numbers_allowed(text: str, facts: dict[str, Any]) -> bool:
    facts_n = _fact_numbers(facts)
    for token in _NUMBER.findall(text):
        try:
            value = float(token)
        except ValueError:
            return False
        if not any(_close(value, fact) or (abs(fact) <= 1 and _close(value, fact * 100)) for fact in facts_n):
            return False
    return True


def _cache_get(key: str) -> Optional[dict[str, Any]]:
    row = _CACHE.get(key)
    if row is None:
        return None
    stamp, payload = row
    if time.monotonic() - stamp > _CACHE_TTL:
        _CACHE.pop(key, None)
        return None
    return dict(payload)


def _cache_put(key: str, payload: dict[str, Any]) -> None:
    if len(_CACHE) >= _CACHE_LIMIT:
        oldest = min(_CACHE, key=lambda item: _CACHE[item][0])
        _CACHE.pop(oldest, None)
    _CACHE[key] = (time.monotonic(), dict(payload))


def run_model(facts: dict[str, Any], client: Optional[ModelClient], *, timeout_s: float = 2.0) -> dict[str, Any]:
    if client is None:
        return model_gate()
    encoded = json.dumps(facts, sort_keys=True, separators=(",", ":"), default=str)
    if len(encoded) > _MAX_INPUT:
        return {"status": "rejected", "reason": "input_limit", "live": False, "external": False, "text": None, "mock": bool(getattr(client, "is_mock", False))}
    key = hashlib.sha256(encoded.encode()).hexdigest()
    cached = _cache_get(key)
    if cached is not None:
        cached["cached"] = True
        return cached
    try:
        raw = client.complete(facts, timeout_s=timeout_s)
    except TimeoutError:
        return {"status": "timeout", "reason": "timeout", "live": False, "external": False, "text": None, "mock": bool(getattr(client, "is_mock", False))}
    except Exception:
        return {"status": "failed", "reason": "client_error", "live": False, "external": False, "text": None, "mock": bool(getattr(client, "is_mock", False))}
    checked = validate_model_output(raw, facts)
    mock = bool(getattr(client, "is_mock", False))
    if checked is None:
        return {"status": "rejected", "reason": "unverified", "live": False, "external": False, "text": None, "mock": mock}
    payload = {
        "status": "mock" if mock else "accepted",
        "reason": None,
        "live": False,
        "external": False,
        "text": checked["summary"],
        "citations": checked["citations"],
        "mock": mock,
        "cached": False,
    }
    _cache_put(key, payload)
    return payload


def facts_for_model(explanation: dict[str, Any]) -> dict[str, Any]:
    """Minimal rule facts. No account id, balance, secret, or free-form note."""
    conditions = []
    for row in explanation.get("conditions") or []:
        if row.get("status") not in {"met", "unmet", "unknown"}:
            continue
        conditions.append({"id": row.get("id"), "status": row.get("status")})
    return {
        "strategy_id": explanation.get("strategy_id"),
        "symbol": explanation.get("symbol"),
        "asof_ts": explanation.get("asof_ts"),
        "side": explanation.get("side"),
        "score": explanation.get("score"),
        "action": explanation.get("action"),
        "reconstructed": explanation.get("reconstructed"),
        "conditions": conditions[:12],
    }
