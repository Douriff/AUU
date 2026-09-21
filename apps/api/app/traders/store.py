"""TraderWatchlist persistence — addresses only, no private keys."""
from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict

from app.models.contracts import TraderWatchlistItem

# Mock-first seed wallets (paper observe). Not real keys.
WATCH_SNIPER = "WatchSniper1111111111111111111111111111111"
WATCH_MID = "WatchMidCurve11111111111111111111111111111"
WATCH_GRAD = "WatchGradChase1111111111111111111111111111"

_LOCK = threading.Lock()
_ITEMS: dict[str, TraderWatchlistItem] = {}
_LOADED = False

_BASE58_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]+$")
_FORBIDDEN_FIELDS = {
    "private_key",
    "secret",
    "secret_key",
    "keypair",
    "mnemonic",
    "seed",
    "wallet_secret",
}


class WatchUpsert(BaseModel):
    """PUT body — addresses / labels only. Extra key material is rejected."""

    model_config = ConfigDict(extra="forbid")

    watch_id: Optional[str] = None
    address: Optional[str] = None
    label: Optional[str] = None
    enabled: Optional[bool] = None
    source: Optional[str] = None
    tags_override: Optional[list[str]] = None
    risk_notes: Optional[str] = None
    items: Optional[list["WatchUpsert"]] = None


def _data_dir() -> Path:
    raw = (os.getenv("TRADER_WATCH_STORE") or "").strip()
    if raw:
        p = Path(raw)
        if p.suffix:
            return p.parent
        return p
    return Path(__file__).resolve().parents[2] / "data"


def watchlist_path() -> Path:
    raw = (os.getenv("TRADER_WATCH_STORE") or "").strip()
    if raw and Path(raw).suffix:
        return Path(raw)
    return _data_dir() / "trader_watchlist.json"


def _now_ms() -> int:
    return int(time.time() * 1000)


def _watch_id_for(address: str) -> str:
    compact = re.sub(r"[^A-Za-z0-9]", "", address)[:16] or "addr"
    return f"watch-{compact.lower()}"


def validate_address(address: str) -> str:
    addr = (address or "").strip()
    if not addr:
        raise ValueError("address required")
    if any(ch.isspace() for ch in addr):
        raise ValueError("address must not contain whitespace")
    if len(addr) < 32 or len(addr) > 48:
        raise ValueError("address length must be 32–48")
    if not _BASE58_RE.match(addr):
        raise ValueError("address must be base58-like (no private key material)")
    return addr


def _default_items(now_ms: Optional[int] = None) -> list[TraderWatchlistItem]:
    ts = now_ms if now_ms is not None else _now_ms()
    return [
        TraderWatchlistItem(
            watch_id="watch-sniper",
            address=WATCH_SNIPER,
            label="mock-sniper",
            enabled=True,
            source="rpc",
            added_ts=ts,
            tags_override=[],
            risk_notes="paper mock — early-curve entries",
        ),
        TraderWatchlistItem(
            watch_id="watch-mid",
            address=WATCH_MID,
            label="mock-mid-curve",
            enabled=True,
            source="rpc",
            added_ts=ts,
            tags_override=[],
            risk_notes="paper mock — mid-curve entries",
        ),
        TraderWatchlistItem(
            watch_id="watch-grad",
            address=WATCH_GRAD,
            label="mock-graduation-chase",
            enabled=True,
            source="rpc",
            added_ts=ts,
            tags_override=[],
            risk_notes="paper mock — late-curve entries",
        ),
    ]


def _env_seed_items(now_ms: int) -> list[TraderWatchlistItem]:
    raw = (os.getenv("TRADER_WATCH_WALLETS") or "").strip()
    if not raw:
        return []
    out: list[TraderWatchlistItem] = []
    for part in raw.split(","):
        addr = part.strip()
        if not addr:
            continue
        try:
            addr = validate_address(addr)
        except ValueError:
            continue
        out.append(
            TraderWatchlistItem(
                watch_id=_watch_id_for(addr),
                address=addr,
                label=None,
                enabled=True,
                source="rpc",
                added_ts=now_ms,
            )
        )
    return out


def _dump() -> None:
    path = watchlist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "copy_trade_enabled": False,
        "items": [i.model_dump() for i in _ITEMS.values()],
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _load_unlocked() -> None:
    global _LOADED, _ITEMS
    if _LOADED:
        return
    path = watchlist_path()
    items: list[TraderWatchlistItem] = []
    if path.is_file():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            rows = raw.get("items") if isinstance(raw, dict) else raw
            if isinstance(rows, list):
                for row in rows:
                    if isinstance(row, dict) and row.get("address"):
                        items.append(TraderWatchlistItem(**row))
        except (OSError, json.JSONDecodeError, ValueError):
            items = []
    now = _now_ms()
    if not items:
        items = _default_items(now)
    by_id: dict[str, TraderWatchlistItem] = {i.watch_id: i for i in items}
    for extra in _env_seed_items(now):
        by_id.setdefault(extra.watch_id, extra)
    _ITEMS = by_id
    _LOADED = True
    _dump()


def _ensure() -> None:
    with _LOCK:
        _load_unlocked()


def list_watches() -> list[TraderWatchlistItem]:
    _ensure()
    with _LOCK:
        return sorted(_ITEMS.values(), key=lambda i: i.added_ts)


def get_watch(watch_id_or_address: str) -> Optional[TraderWatchlistItem]:
    _ensure()
    key = (watch_id_or_address or "").strip()
    if not key:
        return None
    with _LOCK:
        if key in _ITEMS:
            return _ITEMS[key]
        for item in _ITEMS.values():
            if item.address == key:
                return item
    return None


def upsert_watch(body: WatchUpsert | dict[str, Any]) -> list[TraderWatchlistItem]:
    parsed = body if isinstance(body, WatchUpsert) else WatchUpsert.model_validate(body)
    if parsed.items:
        for row in parsed.items:
            _upsert_one(row)
        return list_watches()
    _upsert_one(parsed)
    return list_watches()


def _upsert_one(parsed: WatchUpsert) -> TraderWatchlistItem:
    payload = parsed.model_dump(exclude_none=True)
    for banned in _FORBIDDEN_FIELDS:
        if banned in payload:
            raise ValueError("private key / secret fields are not allowed")
    _ensure()
    with _LOCK:
        existing: Optional[TraderWatchlistItem] = None
        if parsed.watch_id and parsed.watch_id in _ITEMS:
            existing = _ITEMS[parsed.watch_id]
        elif parsed.address:
            for item in _ITEMS.values():
                if item.address == parsed.address.strip():
                    existing = item
                    break
        source = parsed.source or (existing.source if existing else "rpc")
        if source not in {"portal", "rpc", "indexer"}:
            raise ValueError("source must be portal|rpc|indexer")
        if existing is None:
            if not parsed.address:
                raise ValueError("address required")
            addr = validate_address(parsed.address)
            wid = parsed.watch_id or _watch_id_for(addr)
            item = TraderWatchlistItem(
                watch_id=wid,
                address=addr,
                label=parsed.label,
                enabled=True if parsed.enabled is None else parsed.enabled,
                source=source,  # type: ignore[arg-type]
                added_ts=_now_ms(),
                tags_override=list(parsed.tags_override or []),
                risk_notes=parsed.risk_notes,
            )
        else:
            addr = validate_address(parsed.address) if parsed.address else existing.address
            item = existing.model_copy(
                update={
                    "address": addr,
                    "label": parsed.label if parsed.label is not None else existing.label,
                    "enabled": existing.enabled if parsed.enabled is None else parsed.enabled,
                    "source": source,
                    "tags_override": (
                        list(parsed.tags_override)
                        if parsed.tags_override is not None
                        else existing.tags_override
                    ),
                    "risk_notes": (
                        parsed.risk_notes
                        if parsed.risk_notes is not None
                        else existing.risk_notes
                    ),
                }
            )
        _ITEMS[item.watch_id] = item
        _dump()
        return item


def delete_watch(watch_id_or_address: str) -> bool:
    _ensure()
    key = (watch_id_or_address or "").strip()
    with _LOCK:
        target = None
        if key in _ITEMS:
            target = key
        else:
            for wid, item in _ITEMS.items():
                if item.address == key:
                    target = wid
                    break
        if target is None:
            return False
        del _ITEMS[target]
        _dump()
        return True


def reset_watch_store(*, seed: bool = True) -> None:
    """Test helper: drop memory + file, optionally reseed 3 mock wallets."""
    global _LOADED, _ITEMS
    with _LOCK:
        _ITEMS = {}
        _LOADED = False
        path = watchlist_path()
        if path.is_file():
            try:
                path.unlink()
            except OSError:
                pass
        if seed:
            _load_unlocked()
        else:
            _LOADED = True
            _dump()


WatchUpsert.model_rebuild()
