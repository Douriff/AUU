"""Pump.fun account layout constants + a tiny Borsh-style decoder.

Layouts are taken from public-docs / MIT SDK field order. This module does
**not** talk to RPC, load wallets, or build instructions. No AGPL deps.
"""
from __future__ import annotations

import struct
from typing import Any

# Program IDs (read-only reference).
PUMP_PROGRAM_ID = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
PUMP_AMM_PROGRAM_ID = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
PUMP_FEES_PROGRAM_ID = "pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ"
GLOBAL_PDA = "4wTV1YmiEkRvAtNtsSGPtUrqRYQMe5SKy2uB4Jjaxnjf"

# PDA seeds (informational).
BONDING_CURVE_SEED = b"bonding-curve"

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
