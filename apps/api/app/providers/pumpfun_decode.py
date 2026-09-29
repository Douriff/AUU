"""Pump.fun account layout constants + a tiny Borsh-style decoder.

Layouts are taken from public-docs / MIT SDK field order. This module does
**not** talk to RPC, load wallets, or build instructions. No AGPL deps.
"""
from __future__ import annotations

import hashlib
import struct
from typing import Any

# Program IDs (read-only reference).
PUMP_PROGRAM_ID = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
PUMP_AMM_PROGRAM_ID = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
PUMP_FEES_PROGRAM_ID = "pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ"
GLOBAL_PDA = "4wTV1YmiEkRvAtNtsSGPtUrqRYQMe5SKy2uB4Jjaxnjf"

# PDA seeds (informational).
BONDING_CURVE_SEED = b"bonding-curve"

_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"

# Anchor account discriminator is 8 bytes; BondingCurve then (borsh, no padding):
#   virtual_token_reserves u64
#   virtual_sol_reserves   u64
#   real_token_reserves    u64
#   real_sol_reserves      u64
#   token_total_supply     u64
#   complete               bool
#   creator                pubkey (32)
DISCRIMINATOR_LEN = 8
_RESERVES_FMT = "<5Q"  # 5× u64
_RESERVES_SIZE = 40
_COMPLETE_OFFSET = 40
_CREATOR_OFFSET = 41
_CREATOR_SIZE = 32
BONDING_CURVE_MIN_SIZE = DISCRIMINATOR_LEN + _CREATOR_OFFSET + _CREATOR_SIZE


def decode_bonding_curve(data: bytes) -> dict[str, Any]:
    """Decode a BondingCurve account blob. Raises ValueError if too short."""
    if len(data) < DISCRIMINATOR_LEN + _RESERVES_SIZE + 1:
        raise ValueError("bonding-curve account too short")
    body = data[DISCRIMINATOR_LEN:]
    vt, vs, rt, rs, supply = struct.unpack_from(_RESERVES_FMT, body, 0)
    complete = bool(body[_COMPLETE_OFFSET])
    creator = ""
    if len(body) >= _CREATOR_OFFSET + _CREATOR_SIZE:
        creator = body[_CREATOR_OFFSET : _CREATOR_OFFSET + _CREATOR_SIZE].hex()
    return {
        "virtual_token_reserves": int(vt),
        "virtual_sol_reserves": int(vs),
        "real_token_reserves": int(rt),
        "real_sol_reserves": int(rs),
        "token_total_supply": int(supply),
        "complete": complete,
        "creator_hex": creator,
    }


def encode_bonding_curve_body(
    virtual_token_reserves: int,
    virtual_sol_reserves: int,
    real_token_reserves: int,
    real_sol_reserves: int,
    token_total_supply: int,
    complete: bool,
    creator: bytes | None = None,
    discriminator: bytes | None = None,
) -> bytes:
    """Test helper: pack a fake account. Not used on chain."""
    disc = discriminator if discriminator is not None else b"\x00" * DISCRIMINATOR_LEN
    if len(disc) != DISCRIMINATOR_LEN:
        raise ValueError("discriminator must be 8 bytes")
    body = struct.pack(
        _RESERVES_FMT,
        int(virtual_token_reserves),
        int(virtual_sol_reserves),
        int(real_token_reserves),
        int(real_sol_reserves),
        int(token_total_supply),
    )
    body += b"\x01" if complete else b"\x00"
    cre = creator if creator is not None else b"\x00" * _CREATOR_SIZE
    if len(cre) != _CREATOR_SIZE:
        raise ValueError("creator must be 32 bytes")
    return disc + body + cre


def b58encode(raw: bytes) -> str:
    if not raw:
        return ""
    n = int.from_bytes(raw, "big")
    out = []
    while n > 0:
        n, r = divmod(n, 58)
        out.append(_B58_ALPHABET[r])
    pad = 0
    for b in raw:
        if b == 0:
            pad += 1
        else:
            break
    return _B58_ALPHABET[0] * pad + "".join(reversed(out or [_B58_ALPHABET[0]]))


def _borsh_str(buf: bytes, offset: int) -> tuple[str, int]:
    if offset + 4 > len(buf):
        raise ValueError("short borsh string")
    (n,) = struct.unpack_from("<I", buf, offset)
    start = offset + 4
    end = start + n
    if end > len(buf):
        raise ValueError("borsh string overflow")
    return buf[start:end].decode("utf-8", errors="replace"), end


def decode_create_event(data: bytes) -> dict[str, Any] | None:
    """Best-effort Anchor CreateEvent (name, symbol, uri, mint, bondingCurve, user)."""
    body = data[DISCRIMINATOR_LEN:] if len(data) > DISCRIMINATOR_LEN + 32 else data
    try:
        name, o = _borsh_str(body, 0)
        symbol, o = _borsh_str(body, o)
        uri, o = _borsh_str(body, o)
        if o + 96 > len(body):
            return None
        mint = b58encode(body[o : o + 32])
        user = b58encode(body[o + 64 : o + 96])
        return {"name": name, "symbol": symbol, "uri": uri, "mint": mint, "creator": user}
    except (ValueError, UnicodeDecodeError):
        return None


def trade_event_discriminator() -> bytes:
    """Anchor ``event:TradeEvent`` discriminator (sha256, first 8 bytes)."""
    return hashlib.sha256(b"event:TradeEvent").digest()[:DISCRIMINATOR_LEN]


# TradeEvent body after the 8-byte discriminator (borsh, public field order).
# Trailing bytes (newer program versions) are ignored.
# mint, sol_amount, token_amount, is_buy, user, timestamp,
# virtual_sol, virtual_token, real_sol, real_token,
# fee_recipient, fee_basis_points, fee, creator, creator_fee_basis_points, creator_fee
_TRADE_EVENT_BODY = 217


def decode_trade_event(data: bytes) -> dict[str, Any] | None:
    """Decode an Anchor TradeEvent. None when the discriminator or length does not match."""
    if len(data) < DISCRIMINATOR_LEN + _TRADE_EVENT_BODY:
        return None
    if data[:DISCRIMINATOR_LEN] != trade_event_discriminator():
        return None
    o = DISCRIMINATOR_LEN
    mint = b58encode(data[o : o + 32])
    o += 32
    (sol_amount,) = struct.unpack_from("<Q", data, o)
    o += 8
    (token_amount,) = struct.unpack_from("<Q", data, o)
    o += 8
    is_buy = bool(data[o])
    o += 1
    user = b58encode(data[o : o + 32])
    o += 32
    (timestamp,) = struct.unpack_from("<q", data, o)
    o += 8
    vs, vt, rs, rt = struct.unpack_from("<4Q", data, o)
    o += 32
    o += 32  # fee_recipient
    (fee_bps,) = struct.unpack_from("<Q", data, o)
    o += 8
    (fee,) = struct.unpack_from("<Q", data, o)
    o += 8
    creator = b58encode(data[o : o + 32])
    o += 32
    (creator_fee_bps,) = struct.unpack_from("<Q", data, o)
    o += 8
    (creator_fee,) = struct.unpack_from("<Q", data, o)
    if not mint:
        return None
    return {
        "mint": mint,
        "sol_amount": int(sol_amount),
        "token_amount": int(token_amount),
        "is_buy": is_buy,
        "user": user,
        "timestamp": int(timestamp),
        "virtual_sol_reserves": int(vs),
        "virtual_token_reserves": int(vt),
        "real_sol_reserves": int(rs),
        "real_token_reserves": int(rt),
        "fee_basis_points": int(fee_bps),
        "fee": int(fee),
        "creator": creator,
        "creator_fee_basis_points": int(creator_fee_bps),
        "creator_fee": int(creator_fee),
    }


def extract_trade_from_logs(logs: list[str]) -> dict[str, Any] | None:
    """Read-only parse of Program data logs whose discriminator is TradeEvent."""
    import base64

    for line in logs:
        if "Program data:" not in (line or ""):
            continue
        b64 = line.split("Program data:", 1)[-1].strip()
        try:
            raw = base64.b64decode(b64)
        except Exception:
            continue
        parsed = decode_trade_event(raw)
        if parsed and parsed.get("mint"):
            return parsed
    return None


def extract_create_from_logs(logs: list[str]) -> dict[str, Any] | None:
    """Read-only parse of RPC logsSubscribe Create traces."""
    import base64

    is_create = any("Instruction: Create" in (x or "") or "CreateEvent" in (x or "") for x in logs)
    if not is_create:
        return None
    for line in logs:
        if "Program data:" not in (line or ""):
            continue
        b64 = line.split("Program data:", 1)[-1].strip()
        try:
            raw = base64.b64decode(b64)
        except Exception:
            continue
        parsed = decode_create_event(raw)
        if parsed and parsed.get("mint"):
            return parsed
    return None
