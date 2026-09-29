"""Holder-structure and token-safety factors for paper evidence (record only).

Two independent free sources:

* **Tape** (``HolderLedgerBook``): every real pump ``TradeEvent`` the logs
  feed already delivers is folded into a per-mint ledger of
  ``(ts, slot, trader, signed token amount)``. Replaying it up to the signal
  time gives exact holdings *at the signal*, the creation-slot bundle and the
  early-sniper share. It cannot see token transfers or prints the feed missed,
  so it is only trusted when the ledger starts at the mint's ``Create`` event.
* **RPC** (``holder_stats_from_accounts`` / ``parse_mint_account``): one
  ``getAccountInfo`` on the mint (mint / freeze authority, supply) and one
  ``getProgramAccounts`` of the mint's token accounts (owner + amount only via
  ``dataSlice``) on the public Solana RPC. Ground truth including transfers,
  but read a moment after the signal.

Everything here is pure or bounded; nothing is read by the strategy. Values
that cannot be computed are ``None`` with an entry in ``null_reasons``.
"""
from __future__ import annotations

import base64
import sys
import threading
from collections import OrderedDict
from typing import Any, Iterable, Mapping, Optional, Sequence

from app.providers.pumpfun_curve_math import TOKEN_TOTAL_SUPPLY
from app.providers.pumpfun_decode import b58encode

# Slots counted as the creation "bundle": the Create slot and the next one.
BUNDLE_SLOTS = 2
# First-buy window for "early snipers" (~4 s at 400 ms slots), Create slot included.
SNIPER_SLOTS = 10
# Bounded memory: entries happen seconds after Create, so a few thousand prints
# per mint is plenty; beyond that the ledger stops growing and reports overflow.
MAX_LEDGER_MINTS = 128
MAX_LEDGER_EVENTS = 2_000
TOP_N = 10

SPL_TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
SPL_TOKEN_ACCOUNT_SIZE = 165


def _pct(amount: float, supply: float) -> Optional[float]:
    if not supply or supply <= 0:
        return None
    return round(100.0 * float(amount) / float(supply), 4)


# --------------------------------------------------------------------- tape
class HolderLedgerBook:
    """Per-mint append-only trade ledger. Thread-safe and bounded.

    ``apply`` is O(1) and is called by the provider for every real print;
    ``snapshot`` copies the events up to a timestamp for a background worker.
    """

    def __init__(self, *, max_mints: int = MAX_LEDGER_MINTS, max_events: int = MAX_LEDGER_EVENTS) -> None:
        self._lock = threading.Lock()
        self._books: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
        self.max_mints = max_mints
        self.max_events = max_events

    def apply(
        self,
        mint: str,
        *,
        trader: Optional[str],
        side: str,
        token_amount: int,
        slot: Optional[int],
        ts: int,
    ) -> None:
        if not mint:
            return
        with self._lock:
            book = self._books.get(mint)
            if book is None:
                book = {"events": [], "overflow": False, "no_trader": 0, "first_ts": int(ts)}
                self._books[mint] = book
                while len(self._books) > self.max_mints:
                    self._books.popitem(last=False)
            if not trader:
                book["no_trader"] += 1
                return
            if len(book["events"]) >= self.max_events:
                book["overflow"] = True
                return
            signed = int(token_amount) if side == "buy" else -int(token_amount)
            book["events"].append((int(ts), int(slot) if slot is not None else None, sys.intern(str(trader)), signed))

    def drop(self, mint: str) -> None:
        with self._lock:
            self._books.pop(mint, None)

    def snapshot(self, mint: str, upto_ts: Optional[int] = None) -> Optional[dict[str, Any]]:
        with self._lock:
            book = self._books.get(mint)
            if book is None:
                return None
            events = list(book["events"])
            overflow = bool(book["overflow"])
            no_trader = int(book["no_trader"])
            first_ts = int(book["first_ts"])
        if upto_ts is not None:
            events = [e for e in events if e[0] <= int(upto_ts)]
        return {"events": events, "overflow": overflow, "no_trader": no_trader, "first_ts": first_ts}

    def __len__(self) -> int:
        with self._lock:
            return len(self._books)


def tape_holder_factors(
    snapshot: Optional[Mapping[str, Any]],
    *,
    creator: Optional[str],
    created_slot: Optional[int],
    from_create: bool,
    supply: int = TOKEN_TOTAL_SUPPLY,
) -> dict[str, Any]:
    """Holder structure at the signal from the feed's own trade ledger. Pure."""
    reasons: dict[str, str] = {}
    out: dict[str, Any] = {
        "source": "logs_trade_ledger",
        "complete": False,
        "events": 0,
        "holder_count": None,
        "dev_pct": None,
        "top10_pct": None,
        "bundle_pct": None,
        "bundle_pct_incl_dev": None,
        "bundle_held_pct": None,
        "bundle_wallets": None,
        "sniper_pct": None,
        "sniper_held_pct": None,
        "sniper_wallets": None,
        "negative_wallets": None,
        "null_reasons": reasons,
    }
    base_reason = None
    if snapshot is None:
        base_reason = "no_ledger_for_mint"
    elif snapshot.get("overflow"):
        base_reason = "ledger_overflow"
    elif not from_create:
        base_reason = "ledger_not_from_create_event"
    if base_reason:
        for k in ("holder_count", "dev_pct", "top10_pct", "bundle_pct", "sniper_pct"):
            reasons[k] = base_reason
        if snapshot is not None:
            out["events"] = len(snapshot.get("events") or [])
        return out
    events: Sequence[tuple] = list(snapshot.get("events") or [])
    out["events"] = len(events)
    out["complete"] = True
    slots = [int(e[1]) for e in events if e[1] is not None]
    out["last_slot"] = max(slots) if slots else None
    if snapshot.get("no_trader"):
        out["prints_without_trader"] = int(snapshot["no_trader"])
    bal: dict[str, int] = {}
    for _ts, _slot, who, amt in events:
        bal[who] = bal.get(who, 0) + int(amt)
    negative = sum(1 for v in bal.values() if v < 0)
    held = {w: v for w, v in bal.items() if v > 0}
    out["negative_wallets"] = negative
    out["holder_count"] = len(held)
    if creator:
        out["dev_pct"] = _pct(max(0, bal.get(creator, 0)), supply)
    else:
        reasons["dev_pct"] = "creator_unknown"
    out["top10_pct"] = _pct(sum(sorted(held.values(), reverse=True)[:TOP_N]), supply)
    # Bundle / sniper need the Create slot and slots on the prints.
    if created_slot is None:
        reasons["bundle_pct"] = reasons["sniper_pct"] = "no_create_slot"
        return out
    if not events or any(e[1] is None for e in events):
        reasons["bundle_pct"] = reasons["sniper_pct"] = "no_slot_on_prints" if events else "no_prints"
        return out
    c0 = int(created_slot)
    first_slot: dict[str, int] = {}
    for _ts, slot, who, amt in events:
        if amt > 0 and who not in first_slot:
            first_slot[who] = int(slot)
    bundle_buys = [(who, amt) for _ts, slot, who, amt in events if amt > 0 and c0 <= int(slot) < c0 + BUNDLE_SLOTS]
    bundle_wallets = {w for w, _ in bundle_buys if w != creator}
    out["bundle_wallets"] = len(bundle_wallets)
    out["bundle_pct"] = _pct(sum(a for w, a in bundle_buys if w != creator), supply)
    out["bundle_pct_incl_dev"] = _pct(sum(a for _w, a in bundle_buys), supply)
    out["bundle_held_pct"] = _pct(sum(max(0, bal.get(w, 0)) for w in bundle_wallets), supply)
    snipers = {w for w, s in first_slot.items() if w != creator and c0 <= s < c0 + SNIPER_SLOTS}
    out["sniper_wallets"] = len(snipers)
    out["sniper_pct"] = _pct(
        sum(a for _ts, slot, who, a in events if a > 0 and who in snipers and c0 <= int(slot) < c0 + SNIPER_SLOTS),
        supply,
    )
    out["sniper_held_pct"] = _pct(sum(max(0, bal.get(w, 0)) for w in snipers), supply)
    return out


# ---------------------------------------------------------------------- RPC
def parse_mint_account(value: Optional[Mapping[str, Any]]) -> dict[str, Any]:
    """``getAccountInfo(mint, jsonParsed)`` value → authorities / supply. Pure."""
    if not value:
        return {"ok": False, "reason": "mint_account_missing"}
    data = value.get("data")
    parsed = data.get("parsed") if isinstance(data, Mapping) else None
    info = parsed.get("info") if isinstance(parsed, Mapping) else None
    if not isinstance(info, Mapping) or (parsed.get("type") not in (None, "mint")):
        return {"ok": False, "reason": "mint_account_not_parsed"}
    owner = str(value.get("owner") or "")
    mint_auth = info.get("mintAuthority")
    freeze_auth = info.get("freezeAuthority")
    try:
        supply = int(info.get("supply"))
    except (TypeError, ValueError):
        supply = None
    return {
        "ok": True,
        "token_program": owner or None,
        "token_2022": owner == TOKEN_2022_PROGRAM,
        "mint_authority": mint_auth or None,
        "freeze_authority": freeze_auth or None,
        "mint_authority_revoked": not mint_auth,
        "freeze_authority_revoked": not freeze_auth,
        "supply": supply,
        "decimals": info.get("decimals"),
    }


def token_accounts_gpa_params(mint: str, token_program: str) -> list[Any]:
    """``getProgramAccounts`` params: token accounts of ``mint``, owner+amount bytes only."""
    filters: list[dict[str, Any]] = [{"memcmp": {"offset": 0, "bytes": mint}}]
    if token_program == SPL_TOKEN_PROGRAM:
        filters.insert(0, {"dataSize": SPL_TOKEN_ACCOUNT_SIZE})
    return [
        token_program,
        {
            "encoding": "base64",
            "commitment": "confirmed",
            "withContext": True,
            "dataSlice": {"offset": 32, "length": 40},
            "filters": filters,
        },
    ]


def decode_owner_amount_rows(accounts: Iterable[Mapping[str, Any]]) -> list[tuple[str, int]]:
    """GPA rows with a 40-byte slice (owner pubkey + u64 amount) → ``[(owner, amount)]``."""
    out: list[tuple[str, int]] = []
    for acc in accounts or []:
        data = (acc.get("account") or {}).get("data")
        blob = data[0] if isinstance(data, list) and data else data
        try:
            raw = base64.b64decode(str(blob or ""))
        except Exception:
            continue
        if len(raw) < 40:
            continue
        out.append((b58encode(raw[:32]), int.from_bytes(raw[32:40], "little")))
    return out


def holder_stats_from_accounts(
    rows: Iterable[tuple[str, int]],
    *,
    supply: Optional[int],
    curve_owner: Optional[str],
    creator: Optional[str],
) -> dict[str, Any]:
    """Holder count / top-10 / dev share from on-chain token accounts. Pure.

    Balances are aggregated per owner; the bonding-curve PDA's account is
    excluded from holders and from the top 10 and reported as ``curve_pct``.
    """
    by_owner: dict[str, int] = {}
    accounts = 0
    for owner, amount in rows:
        accounts += 1
        if amount > 0:
            by_owner[owner] = by_owner.get(owner, 0) + int(amount)
    total = int(supply or 0) or TOKEN_TOTAL_SUPPLY
    curve_amt = by_owner.pop(curve_owner, 0) if curve_owner else 0
    top = sorted(by_owner.values(), reverse=True)[:TOP_N]
    circulating = total - curve_amt
    reasons: dict[str, str] = {}
    out = {
        "token_accounts": accounts,
        "holder_count": len(by_owner),
        "top10_pct": _pct(sum(top), total),
        "top10_pct_circulating": _pct(sum(top), circulating) if circulating > 0 else None,
        "top1_pct": _pct(top[0], total) if top else 0.0,
        "curve_pct": _pct(curve_amt, total),
        "dev_pct": None,
        "null_reasons": reasons,
    }
    if curve_owner and not curve_amt:
        reasons["curve_pct"] = "curve_account_not_found"
    if creator:
        out["dev_pct"] = _pct(by_owner.get(creator, 0), total)
    else:
        reasons["dev_pct"] = "creator_unknown"
    return out
