"""Short-timeout JSON GET. Callers cache; nothing here touches the paper loop."""
from __future__ import annotations

import json
import os
import socket
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class UpstreamError(Exception):
    def __init__(self, kind: str, detail: str = "") -> None:
        self.kind = kind  # not_found | unavailable
        self.detail = detail
        super().__init__(detail or kind)


def http_timeout() -> float:
    try:
        return max(0.4, float(os.getenv("MARKETDATA_TIMEOUT", "2.5")))
    except ValueError:
        return 2.5


def get_json(url: str, timeout: float | None = None) -> Any:
    req = Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "AUU-paper/0.1",
        },
        method="GET",
    )
    try:
        with urlopen(req, timeout=http_timeout() if timeout is None else timeout) as resp:
            raw = resp.read()
    except HTTPError as exc:
        kind = "not_found" if exc.code in {400, 404} else "unavailable"
        raise UpstreamError(kind, str(exc.code)) from exc
    except (URLError, TimeoutError, socket.timeout, OSError) as exc:
        raise UpstreamError("unavailable", "timeout") from exc
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UpstreamError("unavailable", "parse") from exc
