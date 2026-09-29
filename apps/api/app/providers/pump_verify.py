"""Read-only check that a mint is a live (not graduated) pump.fun bonding curve.

The bonding curve is the pump program PDA ``["bonding-curve", mint]``. A mint
is accepted only when that account exists, is owned by the pump program, and
its ``complete`` flag is false. Reserves from the account seed the live-paper
price so the first mark is the real on-chain state, not a template.

Network access goes through ``rpc_call`` so tests inject a fake. This module
never signs, never sends a transaction, and never logs the RPC URL (it may
carry an API key).
"""
from __future__ import annotations

import base64
import hashlib
import logging
import os
from typing import Any, Callable, Optional

from app.providers.pumpfun_decode import (
    BONDING_CURVE_SEED,
    PUMP_PROGRAM_ID,
    b58encode,
    decode_bonding_curve,
)
from app.wallet.codec import b58decode

log = logging.getLogger("auu.pump_verify")

RpcCall = Callable[[str, list[Any]], Any]

# ed25519 field / curve constants (RFC 8032).
_P = 2**255 - 19
_D = (-121665 * pow(121666, _P - 2, _P)) % _P
_PDA_MARKER = b"ProgramDerivedAddress"


def _on_ed25519_curve(raw: bytes) -> bool:
    """Same test as curve25519-dalek ``CompressedEdwardsY::decompress().is_some()``."""
    if len(raw) != 32:
        return False
    y = int.from_bytes(raw, "little") & ((1 << 255) - 1)
    y %= _P
    y2 = (y * y) % _P
    u = (y2 - 1) % _P
    v = (_D * y2 + 1) % _P
    if u == 0:
        return True
    x2 = (u * pow(v, _P - 2, _P)) % _P
    return pow(x2, (_P - 1) // 2, _P) == 1


def find_program_address(seeds: list[bytes], program_id: str) -> tuple[str, int]:
    """Solana ``find_program_address``: highest bump whose hash is off-curve."""
    program = b58decode(program_id)
    for bump in range(255, -1, -1):
        h = hashlib.sha256()
        for seed in seeds:
            h.update(seed)
        h.update(bytes([bump]))
        h.update(program)
        h.update(_PDA_MARKER)
        digest = h.digest()
        if not _on_ed25519_curve(digest):
            return b58encode(digest), bump
    raise ValueError("no viable program address")


def bonding_curve_address(mint: str) -> str:
    return find_program_address([BONDING_CURVE_SEED, b58decode(mint)], PUMP_PROGRAM_ID)[0]


def default_rpc_url() -> str:
    return (
        os.getenv("LIVE_PAPER_RPC_URL")
        or os.getenv("SOLANA_RPC_URL")
        or "https://api.mainnet-beta.solana.com"
    ).strip()


def http_rpc_call(url: Optional[str] = None, *, timeout: float = 8.0) -> RpcCall:
    """JSON-RPC over HTTPS with httpx. Only used at runtime, never in tests."""

    def _call(method: str, params: list[Any]) -> Any:
        import httpx

        body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        resp = httpx.post(url or default_rpc_url(), json=body, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict) and data.get("error"):
            raise RuntimeError(f"rpc error code={data['error'].get('code')}")
        return data.get("result") if isinstance(data, dict) else None

    return _call


class VerifyResult(dict):
    """``ok`` (bool), ``reason`` (str), optional ``reserves`` / ``bonding_curve``."""

    @property
    def ok(self) -> bool:
        return bool(self.get("ok"))

    @property
    def retry(self) -> bool:
        return bool(self.get("retry"))


def verify_pump_mint(mint: str, rpc_call: RpcCall) -> VerifyResult:
    """Accept only pump-program bonding curves that are not complete.

    ``retry=True`` means the RPC could not answer (network error); callers
    keep the mint pending instead of rejecting it.
    """
    try:
        curve = bonding_curve_address(mint)
    except Exception:
        return VerifyResult(ok=False, retry=False, reason="bad_mint")
    try:
        result = rpc_call("getAccountInfo", [curve, {"encoding": "base64", "commitment": "confirmed"}])
    except Exception as exc:  # network / HTTP / rpc error
        log.info("bonding-curve lookup failed (%s); will retry", type(exc).__name__)
        return VerifyResult(ok=False, retry=True, reason="rpc_unavailable", bonding_curve=curve)
    value = result.get("value") if isinstance(result, dict) else None
    if not value:
        return VerifyResult(ok=False, retry=False, reason="no_bonding_curve", bonding_curve=curve)
    if str(value.get("owner") or "") != PUMP_PROGRAM_ID:
        return VerifyResult(ok=False, retry=False, reason="not_pump_program", bonding_curve=curve)
    data = value.get("data")
    blob = data[0] if isinstance(data, list) and data else data
    try:
        raw = base64.b64decode(str(blob or ""))
        decoded = decode_bonding_curve(raw)
    except Exception:
        return VerifyResult(ok=False, retry=False, reason="bad_curve_account", bonding_curve=curve)
    if decoded["complete"] or int(decoded["real_token_reserves"]) <= 0:
        return VerifyResult(ok=False, retry=False, reason="graduated", bonding_curve=curve)
    if int(decoded["virtual_sol_reserves"]) <= 0 or int(decoded["virtual_token_reserves"]) <= 0:
        return VerifyResult(ok=False, retry=False, reason="empty_curve", bonding_curve=curve)
    return VerifyResult(
        ok=True,
        retry=False,
        reason="pump_curve",
        bonding_curve=curve,
        reserves={
            "virtual_sol": int(decoded["virtual_sol_reserves"]),
            "virtual_token": int(decoded["virtual_token_reserves"]),
            "real_sol": int(decoded["real_sol_reserves"]),
            "real_token": int(decoded["real_token_reserves"]),
            "token_total_supply": int(decoded["token_total_supply"]),
        },
    )
