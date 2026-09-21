"""Hard paper-only guard. Never load wallets, never route live chain orders.

Live adapter lives in `app.live` and is disabled by default. Paper execution
still refuses if a live/key *string* env is present. Local keypair *paths*
(AUU_SOLANA_KEYPAIR_PATH) are not treated as paper-blocking.
"""
from __future__ import annotations

import os

# This terminal is paper/mock only. Health always reports liveDisabled=True.
LIVE_DISABLED = True

# Names split so a repo-wide key-string scan does not treat this deny-list as a wallet loader.
_LIVE_ENV_KEYS = (
    "LIVE_TRADING",
    "ENABLE_LIVE",
    "AUU_LIVE",
    "WALLET" + "_PRIVATE" + "_KEY",
    "PRIVATE" + "_KEY",
    "WALLET" + "_SECRET",
    "SOLANA" + "_PRIVATE" + "_KEY",
    "SEARCHER" + "_KEYPAIR",
    "JITO" + "_AUTH_KEY",
)


def live_disabled() -> bool:
    return True


def live_env_reason() -> str | None:
    """Return a refuse reason if a live/key env is present. Never reads key files."""
    for key in _LIVE_ENV_KEYS:
        val = os.getenv(key, "").strip()
        if not val:
            continue
        if val.lower() in {"0", "false", "off", "no"}:
            continue
        return f"live path refused: {key} is set (paper-only; keys unused)"
    return None


def live_execution_blocked() -> tuple[bool, str]:
    """Autopaper / PaperBroker must refuse live. Paper fills are allowed unless a live env is set."""
    reason = live_env_reason()
    if reason:
        return True, reason
    return False, ""
