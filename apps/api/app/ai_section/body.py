"""Bounded JSON body reader for the AI section.

The 8000-byte cap is enforced while reading the ASGI stream, not after a
full ``request.body()`` buffer is already in memory. Content-Length is an
early reject only; a missing or lying Content-Length still cannot cause
this module to keep aggregating past the cap. Unexpected server errors are
not converted into a 400.
"""
from __future__ import annotations

import json
import math
from typing import Any, AsyncIterator, Mapping, Protocol

MAX_BODY = 8000
MAX_JSON_DEPTH = 8
MAX_JSON_CONTAINER = 64


class StreamRequest(Protocol):
    headers: Mapping[str, str]

    def stream(self) -> AsyncIterator[bytes]:
        ...


class BodyReadResult:
    __slots__ = ("status", "data", "bytes_consumed", "chunks_consumed", "stopped_early")

    def __init__(self, status: str, data: dict[str, Any] | None, bytes_consumed: int, chunks_consumed: int, stopped_early: bool):
        self.status = status  # ok | too_large | bad
        self.data = data
        self.bytes_consumed = bytes_consumed
        self.chunks_consumed = chunks_consumed
        self.stopped_early = stopped_early


def _content_length(headers: Mapping[str, Any]) -> int | None:
    raw = headers.get("content-length") if hasattr(headers, "get") else None
    if raw is None and hasattr(headers, "items"):
        for key, value in headers.items():
            if str(key).lower() == "content-length":
                raw = value
                break
    if raw is None:
        return None
    text = raw if isinstance(raw, str) else (raw.decode("ascii", "strict") if isinstance(raw, (bytes, bytearray)) else str(raw))
    if not text.isdigit():
        return None
    return int(text)


def _reject_constant(token: str):
    raise ValueError("non_standard_json_constant")


def _bounded_walk(value: Any, depth: int = 0) -> None:
    if depth > MAX_JSON_DEPTH:
        raise ValueError("json_too_deep")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("non_finite")
    if isinstance(value, int) and value.bit_length() > 256:
        raise ValueError("int_too_big")
    if isinstance(value, dict):
        if len(value) > MAX_JSON_CONTAINER:
            raise ValueError("json_too_wide")
        for item in value.values():
            _bounded_walk(item, depth + 1)
    elif isinstance(value, list):
        if len(value) > MAX_JSON_CONTAINER:
            raise ValueError("json_too_wide")
        for item in value:
            _bounded_walk(item, depth + 1)


def parse_user_json(raw: bytes) -> dict[str, Any] | None:
    """Return a dict, or None when the payload is not acceptable user JSON."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if not text.strip():
        return {}
    try:
        parsed = json.loads(text, parse_constant=_reject_constant, strict=True)
        _bounded_walk(parsed)
    except (json.JSONDecodeError, ValueError, RecursionError, TypeError):
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


async def read_bounded_json(request: StreamRequest) -> BodyReadResult:
    length = _content_length(getattr(request, "headers", {}) or {})
    if length is not None and length > MAX_BODY:
        return BodyReadResult("too_large", None, 0, 0, True)

    buf = bytearray()
    consumed = 0
    chunks = 0
    stopped = False
    async for chunk in request.stream():
        chunks += 1
        piece = chunk if isinstance(chunk, (bytes, bytearray)) else bytes(chunk)
        if consumed + len(piece) > MAX_BODY:
            # This overflowing chunk was already delivered by the iterator.
            # Do not append it, and do not pull any further chunks.
            consumed += len(piece)
            stopped = True
            return BodyReadResult("too_large", None, consumed, chunks, True)
        buf.extend(piece)
        consumed += len(piece)
    if not buf:
        return BodyReadResult("ok", {}, consumed, chunks, stopped)
    parsed = parse_user_json(bytes(buf))
    if parsed is None:
        return BodyReadResult("bad", None, consumed, chunks, False)
    return BodyReadResult("ok", parsed, consumed, chunks, False)
