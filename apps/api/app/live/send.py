"""Send gate — independent of liveEnabled / liveDisabled.

Intent build (app.live.intent, official pump-sdk methods only) can be described
without this gate. Submitting a signed payload requires LIVE_SEND_WIRED, which
is False in this scaffold. Default runtime sends zero chain txs.
"""
from __future__ import annotations

from app.live.gate import LIVE_SEND_WIRED

REASON_LIVE_STUB = "LIVE_STUB"


def send_wired() -> bool:
    return bool(LIVE_SEND_WIRED)


def send_allowed() -> bool:
    """Send is a separate latch from liveEnabled. Always false in this PR."""
    return send_wired()


def refuse_send() -> tuple[list[str], str]:
    return (
        [REASON_LIVE_STUB],
        "send gate closed; official pump-sdk intent is documented, not submitted",
    )
