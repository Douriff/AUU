"""Helius / RPC readonly reader — P0 when leaving mock.

Path: user Watchlist wallets → parsed Pump ix (Helius enhanced or RPC-shaped)
→ ctx.pump for current progress/phase → TraderSnapshot.

Live HTTP is off unless TRADER_WATCH_LIVE_FETCH=1 and a server-side key/RPC URL
is set. Default remains mock. Never submits a signed tx. Never scrape Pump frontend.
"""
from __future__ import annotations

import json
import os
from typing import Any, Optional
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from app.models.contracts import TraderSnapshot, TraderWatchlistItem
from app.traders.pump_ix import parse_pump_ix_rows

HELIUS_PARSED_TX_PATH = "/v0/addresses/{address}/transactions"
DEFAULT_HELIUS_HOST = "https://api.helius.xyz"


def reader_mode() -> str:
    raw = (os.getenv("TRADER_WATCH_READER") or "mock").strip().lower()
    if raw == "rpc":
        return "rpc"
    if raw in {"helius", "indexer"}:
        return "helius"
    return "mock"


def helius_enabled() -> bool:
    return reader_mode() in {"helius", "rpc"}


def _api_key_present() -> bool:
    return bool((os.getenv("HELIUS_API_KEY") or "").strip())


def _rpc_url() -> str:
    return (os.getenv("SOLANA_RPC_URL") or "").strip()


def live_fetch_enabled() -> bool:
    flag = (os.getenv("TRADER_WATCH_LIVE_FETCH") or "").strip().lower()
    if flag not in {"1", "true", "yes", "on"}:
        return False
    if reader_mode() == "rpc":
        return bool(_rpc_url())
    return _api_key_present()


def _http_get_json(url: str, timeout: float = 8.0) -> Any:
    req = Request(url, headers={"Accept": "application/json"})
    with urlopen(req, timeout=timeout) as resp:  # noqa: S310 — host is Helius readonly
        return json.loads(resp.read().decode("utf-8"))


def _http_rpc_json(url: str, method: str, params: list[Any], timeout: float = 8.0) -> Any:
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode("utf-8")
    req = Request(
        url,
        data=payload,
        method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    with urlopen(req, timeout=timeout) as resp:  # noqa: S310 — user RPC, readonly methods only
        body = json.loads(resp.read().decode("utf-8"))
    if not isinstance(body, dict):
        return None
    return body.get("result")


class HeliusTraderReader:
    """Readonly Pump ix → Snapshot. LIVE_FETCH class flag is the paper default (off)."""

    LIVE_FETCH = False

    def __init__(self, name: Optional[str] = None) -> None:
        self.name = name or reader_mode()
        self._key_configured = _api_key_present()

    def key_configured(self) -> bool:
        return self._key_configured

    def fetch_parsed_rows(self, address: str) -> list[dict[str, Any]]:
        if not live_fetch_enabled():
            return []
        if self.name == "rpc":
            return self._fetch_rpc_rows(address)
        return self._fetch_helius_rows(address)

    def _fetch_helius_rows(self, address: str) -> list[dict[str, Any]]:
        key = (os.getenv("HELIUS_API_KEY") or "").strip()
        if not key:
            return []
        host = (os.getenv("HELIUS_API_URL") or DEFAULT_HELIUS_HOST).rstrip("/")
        if "helius" not in host.lower():
            return []
        path = HELIUS_PARSED_TX_PATH.format(address=address)
        qs = urlencode({"api-key": key})
        try:
            data = _http_get_json(f"{host}{path}?{qs}")
        except (URLError, TimeoutError, OSError, json.JSONDecodeError, ValueError):
            return []
        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict)]
        if isinstance(data, dict) and isinstance(data.get("result"), list):
            return [x for x in data["result"] if isinstance(x, dict)]
        return []

    def _fetch_rpc_rows(self, address: str) -> list[dict[str, Any]]:
        url = _rpc_url()
        if not url:
            return []
        try:
            sigs = _http_rpc_json(url, "getSignaturesForAddress", [address, {"limit": 40}])
        except (URLError, TimeoutError, OSError, json.JSONDecodeError, ValueError):
            return []
        rows: list[dict[str, Any]] = []
        for item in sigs or []:
            if not isinstance(item, dict) or not item.get("signature"):
                continue
            try:
                tx = _http_rpc_json(
                    url,
                    "getTransaction",
                    [item["signature"], {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}],
                )
            except (URLError, TimeoutError, OSError, json.JSONDecodeError, ValueError):
                continue
            if isinstance(tx, dict):
                tx.setdefault("signature", item["signature"])
                rows.append(tx)
        return rows

    def fetch_snapshot(
        self,
        item: TraderWatchlistItem,
        *,
        now_ms: Optional[int] = None,
        events: Optional[list[dict[str, Any]]] = None,
        pump_by_mint: Optional[dict[str, dict[str, Any]]] = None,
    ) -> TraderSnapshot:
        from app.traders.snapshot import assemble_from_pump_events

        rows = events if events is not None else self.fetch_parsed_rows(item.address)
        trades = parse_pump_ix_rows(rows, item.address)
        return assemble_from_pump_events(
            item,
            trades,
            now_ms=now_ms,
            pump_by_mint=pump_by_mint,
        )
