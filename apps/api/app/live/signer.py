"""LocalSigner — load a Solana JSON keypair from a local filesystem path.

Expected file: gitignored secrets/live-keypair.json as a JSON array of 64
ints (Solana CLI / Phantom base58 converted locally). Never accepts a secret
string from HTTP or Settings. Never logs or returns secret bytes.

Health may expose keypairMounted (bool) and the public key only
(example 8fs58PRKhWy8jVkm7Ro6umY2jxbjtoY33LjyUb6YakFi). This PR does not
sign or submit chain transactions.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

log = logging.getLogger("auu.live.signer")

REASON_NO_KEYPAIR = "NO_KEYPAIR"

_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _b58encode(data: bytes) -> str:
    n = int.from_bytes(data, "big")
    out: list[str] = []
    while n > 0:
        n, r = divmod(n, 58)
        out.append(_B58[r])
    pad = 0
    for b in data:
        if b == 0:
            pad += 1
        else:
            break
    body = "".join(reversed(out)) if out else ""
    return (_B58[0] * pad) + (body or (_B58[0] if not pad else ""))


def _b58decode(s: str) -> bytes:
    n = 0
    for ch in s:
        i = _B58.find(ch)
        if i < 0:
            return b""
        n = n * 58 + i
    pad = 0
    for ch in s:
        if ch == _B58[0]:
            pad += 1
        else:
            break
    if n == 0:
        body = b""
    else:
        length = (n.bit_length() + 7) // 8
        body = n.to_bytes(length, "big")
    return (b"\x00" * pad) + body


@dataclass(frozen=True)
class SignerStatus:
    ok: bool
    reason: str
    present: bool
    pubkey: str = ""


def _as_byte(item: object) -> Optional[int]:
    if isinstance(item, bool) or item is None:
        return None
    if isinstance(item, int) and 0 <= item <= 255:
        return item
    if isinstance(item, str) and item.strip().isdigit():
        n = int(item.strip())
        if 0 <= n <= 255:
            return n
    return None


def _coerce_blob(data: object) -> Optional[list[int]]:
    """Accept Solana CLI [64 ints] or a local Phantom-base58 conversion.

    Never logs `data`.
    """
    if isinstance(data, list):
        out: list[int] = []
        for item in data:
            n = _as_byte(item)
            if n is None:
                return None
            out.append(n)
        if len(out) in (32, 64):
            return out
        return None
    if isinstance(data, str):
        raw = _b58decode(data.strip())
        if len(raw) in (32, 64):
            return list(raw)
        return None
    if isinstance(data, dict) and data:
        for key in ("secretKey", "keypair", "value", "data"):
            if key in data:
                return _coerce_blob(data[key])
        if len(data) == 1:
            return _coerce_blob(next(iter(data.values())))
    return None


def _pubkey_from_blob(data: list[int]) -> str:
    """64-byte Solana arrays store the public key in the last 32 bytes."""
    if len(data) != 64:
        return ""
    pub = bytes(int(x) & 0xFF for x in data[32:64])
    full = _b58encode(pub)
    pub = b""
    return full


class LocalSigner:
    """Filesystem-only signer stub. Drops secret bytes after inspect."""

    def inspect(self, path: Optional[str]) -> SignerStatus:
        if not path or not str(path).strip():
            log.info("local signer: keypair path unset")
            return SignerStatus(ok=False, reason=REASON_NO_KEYPAIR, present=False)
        target = Path(str(path).strip()).expanduser()
        if not target.is_file():
            log.info("local signer: keypair file missing")
            return SignerStatus(ok=False, reason=REASON_NO_KEYPAIR, present=False)
        try:
            raw_text = target.read_text(encoding="utf-8-sig")
        except OSError:
            log.info("local signer: keypair file unreadable")
            return SignerStatus(ok=False, reason=REASON_NO_KEYPAIR, present=False)
        try:
            parsed: object = json.loads(raw_text)
        except json.JSONDecodeError:
            parsed = raw_text.strip()
        blob = _coerce_blob(parsed)
        pubkey = ""
        ok = blob is not None
        if ok and blob is not None:
            pubkey = _pubkey_from_blob(blob)
        # Drop secret material before returning. Do not interpolate into logs.
        blob = None
        parsed = None
        raw_text = ""
        if not ok:
            log.info("local signer: keypair file has invalid shape")
            return SignerStatus(ok=False, reason=REASON_NO_KEYPAIR, present=False)
        log.info("local signer: keypair file present")
        return SignerStatus(ok=True, reason="", present=True, pubkey=pubkey)

    def sign_message(self, message: bytes) -> bytes:
        """Not wired. A later PR may sign with the filesystem keypair after the live gate."""
        raise RuntimeError("LocalSigner.sign_message is not implemented (live scaffold; no chain submit)")
