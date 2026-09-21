"""Parse Pump.fun buy/sell from Helius enhanced txs or RPC-shaped dicts.

Read-only. No HTTP, no chain submit, no frontend scrape.
Program ids match `app.providers.pumpfun_decode`.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Optional

from app.providers.pumpfun_decode import PUMP_AMM_PROGRAM_ID, PUMP_PROGRAM_ID

LAMPORTS = 1_000_000_000
PUMP_PROGRAMS = {PUMP_PROGRAM_ID, PUMP_AMM_PROGRAM_ID}


@dataclass(frozen=True)
class PumpIxTrade:
    """Normalized Pump ix / parsed event (watchlist wallet only)."""

    ts: int
    mint: str
    side: str  # buy | sell
    sol_amount: float
    signature: Optional[str] = None
    slot: Optional[int] = None
    progress_bps: Optional[int] = None
    token_amount: Optional[float] = None


def _as_int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _ts_ms(raw: Any) -> int:
    n = _as_int(raw)
    if n is None:
        return 0
    if n < 10_000_000_000:
        return n * 1000
    return n


def _wallet_match(left: Optional[str], right: str) -> bool:
    if not left or not right:
        return False
    return str(left).strip() == right.strip()


def _is_pump_source(row: dict[str, Any]) -> bool:
    source = str(row.get("source") or row.get("type") or "").upper()
    if "PUMP" in source:
        return True
    for ix in row.get("instructions") or []:
        if not isinstance(ix, dict):
            continue
        pid = str(ix.get("programId") or ix.get("program_id") or "")
        if pid in PUMP_PROGRAMS:
            return True
    tx = row.get("transaction") if isinstance(row.get("transaction"), dict) else {}
    msg = tx.get("message") if isinstance(tx.get("message"), dict) else {}
    for ix in msg.get("instructions") or []:
        if not isinstance(ix, dict):
            continue
        pid = str(ix.get("programId") or ix.get("program") or "")
        if pid in PUMP_PROGRAMS:
            return True
    return False


def _sol_from_native(row: dict[str, Any], wallet: str, *, outbound: bool) -> float:
    total = 0.0
    for tr in row.get("nativeTransfers") or row.get("native_transfers") or []:
        if not isinstance(tr, dict):
            continue
        amount = _as_float(tr.get("amount") if tr.get("amount") is not None else tr.get("lamports")) or 0.0
        if amount > 1e6:
            amount = amount / LAMPORTS
        src = str(tr.get("fromUserAccount") or tr.get("from") or "")
        dst = str(tr.get("toUserAccount") or tr.get("to") or "")
        if outbound and _wallet_match(src, wallet):
            total += amount
        if not outbound and _wallet_match(dst, wallet):
            total += amount
    return total


def _token_for_wallet(row: dict[str, Any], wallet: str, *, inbound: bool) -> tuple[Optional[str], Optional[float]]:
    for tr in row.get("tokenTransfers") or row.get("token_transfers") or []:
        if not isinstance(tr, dict):
            continue
        mint = str(tr.get("mint") or "")
        if not mint:
            continue
        src = str(tr.get("fromUserAccount") or tr.get("from") or "")
        dst = str(tr.get("toUserAccount") or tr.get("to") or "")
        qty = _as_float(tr.get("tokenAmount") if tr.get("tokenAmount") is not None else tr.get("amount"))
        if inbound and _wallet_match(dst, wallet):
            return mint, qty
        if not inbound and _wallet_match(src, wallet):
            return mint, qty
    return None, None


def _from_normalized(row: dict[str, Any], wallet: str) -> Optional[PumpIxTrade]:
    mint = str(row.get("mint") or "")
    side = str(row.get("side") or "").lower()
    sol = _as_float(row.get("sol_amount"))
    if not mint or side not in {"buy", "sell"} or sol is None:
        return None
    fee_payer = str(row.get("feePayer") or row.get("wallet") or row.get("address") or wallet)
    if wallet and not _wallet_match(fee_payer, wallet) and row.get("feePayer"):
        return None
    return PumpIxTrade(
        ts=_ts_ms(row.get("ts") if row.get("ts") is not None else row.get("timestamp")),
        mint=mint,
        side=side,
        sol_amount=float(sol),
        signature=str(row.get("signature")) if row.get("signature") else None,
        slot=_as_int(row.get("slot")),
        progress_bps=_as_int(row.get("progress_bps")),
        token_amount=_as_float(row.get("token_amount") if row.get("token_amount") is not None else row.get("tokenAmount")),
    )


def parse_pump_ix_row(row: dict[str, Any], wallet: str) -> Optional[PumpIxTrade]:
    """One Helius enhanced tx, RPC-shaped dict, or already-normalized trade."""
    if not isinstance(row, dict):
        return None
    direct = _from_normalized(row, wallet)
    if direct is not None:
        return direct
    if not _is_pump_source(row):
        return None
    fee_payer = str(row.get("feePayer") or row.get("fee_payer") or "")
    if fee_payer and not _wallet_match(fee_payer, wallet):
        return None
    buy_sol = _sol_from_native(row, wallet, outbound=True)
    sell_sol = _sol_from_native(row, wallet, outbound=False)
    if buy_sol > 0 and buy_sol >= sell_sol:
        mint, token_qty = _token_for_wallet(row, wallet, inbound=True)
        side = "buy"
        sol = buy_sol
    elif sell_sol > 0:
        mint, token_qty = _token_for_wallet(row, wallet, inbound=False)
        side = "sell"
        sol = sell_sol
    else:
        return None
    if not mint:
        return None
    desc = str(row.get("description") or "").lower()
    if "sell" in desc and side == "buy" and sell_sol > 0:
        side = "sell"
        sol = sell_sol
        mint, token_qty = _token_for_wallet(row, wallet, inbound=False)
        if not mint:
            return None
    return PumpIxTrade(
        ts=_ts_ms(row.get("timestamp") if row.get("timestamp") is not None else row.get("blockTime")),
        mint=mint,
        side=side,
        sol_amount=round(float(sol), 8),
        signature=str(row.get("signature")) if row.get("signature") else None,
        slot=_as_int(row.get("slot")),
        progress_bps=_as_int(row.get("progress_bps")),
        token_amount=token_qty,
    )


def parse_pump_ix_rows(rows: Iterable[Any], wallet: str) -> list[PumpIxTrade]:
    out: list[PumpIxTrade] = []
    for row in rows:
        parsed = parse_pump_ix_row(row, wallet) if isinstance(row, dict) else None
        if parsed is not None:
            out.append(parsed)
    out.sort(key=lambda t: t.ts)
    return out
