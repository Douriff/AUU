"""LocalSigner — load a Solana JSON keypair from a filesystem path on this machine.

Never accepts a private-key string from env. Never logs secret bytes.
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


@dataclass(frozen=True)
class SignerStatus:
    ok: bool
    reason: str
    present: bool


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


class LocalSigner:
    """Filesystem-only signer stub.

    `inspect(path)` checks that a keypair file exists and looks like a Solana
    JSON secret array, then drops the bytes. It never retains or prints them.

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
        # Drop secret material before returning. Do not interpolate parsed into logs.
        parsed = None
        raw_text = ""
        if not ok:
            log.info("local signer: keypair file has invalid shape")
            return SignerStatus(ok=False, reason=REASON_NO_KEYPAIR, present=False)
        log.info("local signer: keypair file present")
        return SignerStatus(ok=True, reason="", present=True)

    def sign_message(self, message: bytes) -> bytes:
        """Not wired. A later PR may sign with the filesystem keypair after the live gate."""
        raise RuntimeError("LocalSigner.sign_message is not implemented (live scaffold; no chain submit)")
