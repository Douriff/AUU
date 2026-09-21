"""LocalSigner — load a Solana JSON keypair from a local filesystem path.

Never accepts a secret string from env, HTTP, or Settings. Never logs or
returns secret bytes. Health may expose keypairMounted (bool) and a shortened
public key only.
This PR does not sign or submit chain transactions.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

log = logging.getLogger("auu.live.signer")

REASON_NO_KEYPAIR = "NO_KEYPAIR"

# Bitcoin/Solana alphabet. Used only to shorten the 32-byte public key.
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


def shorten_pubkey(full: str) -> str:
    if len(full) <= 12:
        return full
    return f"{full[:4]}…{full[-4:]}"


@dataclass(frozen=True)
class SignerStatus:
    ok: bool
    reason: str
    present: bool
    pubkey_short: str = ""


def _looks_like_keypair_blob(data: object) -> bool:
    """Solana CLI id.json is a JSON array of 64 (or 32) byte values."""
    if not isinstance(data, list):
        return False
    if len(data) not in (32, 64):
        return False
    for item in data:
        if not isinstance(item, int) or item < 0 or item > 255:
            return False
    return True


def _pubkey_short_from_blob(data: list) -> str:
    """Solana CLI 64-byte arrays store the public key in the last 32 bytes."""
    if len(data) != 64:
        return ""
    pub = bytes(int(x) & 0xFF for x in data[32:64])
    full = _b58encode(pub)
    return shorten_pubkey(full)


class LocalSigner:
    """Filesystem-only signer stub.

    `inspect(path)` checks that a keypair file exists and looks like a Solana
    JSON secret array, derives a shortened pubkey, then drops the bytes.
    It never retains or prints secret material.

    `sign_message` is intentionally unimplemented in this PR.
    """

    def inspect(self, path: Optional[str]) -> SignerStatus:
        if not path or not str(path).strip():
            log.info("local signer: keypair path unset")
            return SignerStatus(ok=False, reason=REASON_NO_KEYPAIR, present=False)
        target = Path(str(path).strip()).expanduser()
        if not target.is_file():
            log.info("local signer: keypair file missing")
            return SignerStatus(ok=False, reason=REASON_NO_KEYPAIR, present=False)
        try:
            raw_text = target.read_text(encoding="utf-8")
        except OSError:
            log.info("local signer: keypair file unreadable")
            return SignerStatus(ok=False, reason=REASON_NO_KEYPAIR, present=False)
        try:
            parsed = json.loads(raw_text)
        except json.JSONDecodeError:
            log.info("local signer: keypair file is not JSON")
            return SignerStatus(ok=False, reason=REASON_NO_KEYPAIR, present=False)
        ok = _looks_like_keypair_blob(parsed)
        pubkey_short = ""
        if ok and isinstance(parsed, list):
            pubkey_short = _pubkey_short_from_blob(parsed)
        # Drop secret material before returning. Do not interpolate parsed into logs.
        parsed = None
        raw_text = ""
        if not ok:
            log.info("local signer: keypair file has invalid shape")
            return SignerStatus(ok=False, reason=REASON_NO_KEYPAIR, present=False)
        log.info("local signer: keypair file present")
        return SignerStatus(ok=True, reason="", present=True, pubkey_short=pubkey_short)

    def sign_message(self, message: bytes) -> bytes:
        """Not wired. A later PR may sign with the filesystem keypair after the live gate."""
        raise RuntimeError("LocalSigner.sign_message is not implemented (live scaffold; no chain submit)")
