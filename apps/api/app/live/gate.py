"""LiveDisabled hard gate — fail closed. This PR does not submit chain transactions.

Locked live caps (user-authorized, not placeholders):
  max_notional_sol = 1.0
  max_day_loss_pct = 0.045
  max_open_mints = 10

Arming still requires:
  1. live_disabled switch OFF (env AUU_LIVE_DISABLED + settings; default ON)
  2. explicit live_armed=true (env AUU_LIVE_ARMED + settings; default false)
  3. local keypair file at AUU_SOLANA_KEYPAIR_PATH (never a private-key string)

LIVE_SEND_WIRED is False in this scaffold, so health.liveDisabled stays true
even if the arm checklist later passes.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Optional

from app.live.signer import LocalSigner, SignerStatus

ENV_LIVE_DISABLED = "AUU_LIVE_DISABLED"
ENV_LIVE_ARMED = "AUU_LIVE_ARMED"
ENV_KEYPAIR_PATH = "AUU_SOLANA_KEYPAIR_PATH"
ENV_MAX_NOTIONAL_SOL = "AUU_LIVE_MAX_NOTIONAL_SOL"
ENV_MAX_DAY_LOSS_PCT = "AUU_LIVE_MAX_DAY_LOSS_PCT"
ENV_MAX_OPEN_MINTS = "AUU_LIVE_MAX_OPEN_MINTS"

REASON_LIVE_DISABLED = "LIVE_DISABLED"
REASON_NO_KEYPAIR = "NO_KEYPAIR"
REASON_LIMITS_MISSING = "LIMITS_MISSING"

# User-authorized live caps. Env/settings may only tighten (never loosen or zero).
LOCKED_MAX_NOTIONAL_SOL = 1.0
LOCKED_MAX_DAY_LOSS_PCT = 0.045
LOCKED_MAX_OPEN_MINTS = 10

# This scaffold never wires a chain submit. Flip only in a later PR that
# actually calls official @pump-fun/pump-sdk after the same hard gates.
LIVE_SEND_WIRED = False

_TRUE = {"1", "true", "on", "yes"}
_FALSE = {"0", "false", "off", "no"}


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    val = str(raw).strip().lower()
    if val in _TRUE:
        return True
    if val in _FALSE:
        return False
    return default


def _parse_positive_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    return n


def _parse_positive_int(value: Any) -> Optional[int]:
    n = _parse_positive_float(value)
    if n is None:
        return None
    i = int(n)
    if i <= 0:
        return None
    return i


def _env_positive_float(name: str) -> Optional[float]:
    return _parse_positive_float(os.getenv(name, ""))


def _env_positive_int(name: str) -> Optional[int]:
    return _parse_positive_int(os.getenv(name, ""))


def clamp_live_notional(value: Any) -> float:
    n = _parse_positive_float(value)
    if n is None:
        return LOCKED_MAX_NOTIONAL_SOL
    return min(n, LOCKED_MAX_NOTIONAL_SOL)


def clamp_live_day_loss_pct(value: Any) -> float:
    n = _parse_positive_float(value)
    if n is None:
        return LOCKED_MAX_DAY_LOSS_PCT
    return min(n, LOCKED_MAX_DAY_LOSS_PCT)


def clamp_live_open_mints(value: Any) -> int:
    n = _parse_positive_int(value)
    if n is None:
        return LOCKED_MAX_OPEN_MINTS
    return min(n, LOCKED_MAX_OPEN_MINTS)


@dataclass
class LiveLimits:
    max_notional_sol: float = LOCKED_MAX_NOTIONAL_SOL
    max_day_loss_pct: float = LOCKED_MAX_DAY_LOSS_PCT
    max_open_mints: int = LOCKED_MAX_OPEN_MINTS

    def missing_names(self) -> list[str]:
        missing: list[str] = []
        if self.max_notional_sol is None or self.max_notional_sol <= 0:
            missing.append("max_notional_sol")
        if self.max_day_loss_pct is None or self.max_day_loss_pct <= 0:
            missing.append("max_day_loss_pct")
        if self.max_open_mints is None or self.max_open_mints <= 0:
            missing.append("max_open_mints")
        return missing

    def complete(self) -> bool:
        return not self.missing_names()

    def as_dict(self) -> dict[str, float | int]:
        return {
            "max_notional_sol": self.max_notional_sol,
            "max_day_loss_pct": self.max_day_loss_pct,
            "max_open_mints": self.max_open_mints,
        }


@dataclass
class LiveOverlay:
    """In-process Settings overlay. Limits are locked; overlay cannot loosen them."""

    live_disabled: Optional[bool] = None
    live_armed: Optional[bool] = None


@dataclass
class LiveStatus:
    live_disabled: bool
    live_armed: bool
    live_send_wired: bool
    reasons: list[str]
    keypair_configured: bool
    keypair_env: str
    limits: LiveLimits
    limits_missing: list[str]
    disabled_switch: bool
    armed_flag: bool

    @property
    def armed(self) -> bool:
        """True only when every arm condition holds (still no chain submit here)."""
        return not self.reasons

    def primary_reason(self) -> str:
        for code in (REASON_LIVE_DISABLED, REASON_NO_KEYPAIR, REASON_LIMITS_MISSING):
            if code in self.reasons:
                return code
        return REASON_LIVE_DISABLED

    def as_dict(self) -> dict[str, Any]:
        return {
            "liveDisabled": self.live_disabled,
            "liveArmed": self.live_armed,
            "liveSendWired": self.live_send_wired,
            "reasons": list(self.reasons),
            "keypairConfigured": self.keypair_configured,
            "keypairEnv": self.keypair_env,
            "keypairPathHint": (
                "set AUU_SOLANA_KEYPAIR_PATH on this machine (gitignored .env); "
                "never paste a private key; never upload the file"
            ),
            "limits": self.limits.as_dict(),
            "limitsLocked": True,
            "limitsMissing": list(self.limits_missing),
            "disabledSwitch": self.disabled_switch,
            "armedFlag": self.armed_flag,
            "venue": "live",
            "sendEnabled": False,
        }


_overlay = LiveOverlay()
_signer = LocalSigner()


def get_overlay() -> LiveOverlay:
    return _overlay


def reset_live_state() -> None:
    global _overlay, _signer
    _overlay = LiveOverlay()
    _signer = LocalSigner()


def keypair_path() -> str:
    return (os.getenv(ENV_KEYPAIR_PATH) or "").strip()


def inspect_keypair() -> SignerStatus:
    return _signer.inspect(keypair_path() or None)


def effective_limits() -> LiveLimits:
    """Locked caps; env may only tighten. Zero/unset env falls back to locked values."""
    return LiveLimits(
        max_notional_sol=clamp_live_notional(_env_positive_float(ENV_MAX_NOTIONAL_SOL)),
        max_day_loss_pct=clamp_live_day_loss_pct(_env_positive_float(ENV_MAX_DAY_LOSS_PCT)),
        max_open_mints=clamp_live_open_mints(_env_positive_int(ENV_MAX_OPEN_MINTS)),
    )


def disabled_switch() -> bool:
    if _overlay.live_disabled is not None:
        return bool(_overlay.live_disabled)
    return _env_bool(ENV_LIVE_DISABLED, True)


def armed_flag() -> bool:
    if _overlay.live_armed is not None:
        return bool(_overlay.live_armed)
    return _env_bool(ENV_LIVE_ARMED, False)


def evaluate() -> LiveStatus:
    limits = effective_limits()
    missing = limits.missing_names()
    signer = inspect_keypair()
    switch = disabled_switch()
    flag = armed_flag()
    reasons: list[str] = []
    if switch or not flag:
        reasons.append(REASON_LIVE_DISABLED)
    if not signer.ok:
        reasons.append(REASON_NO_KEYPAIR)
    if missing:
        reasons.append(REASON_LIMITS_MISSING)
    gate_armed = not reasons
    live_disabled = (not gate_armed) or (not LIVE_SEND_WIRED)
    return LiveStatus(
        live_disabled=live_disabled,
        live_armed=gate_armed,
        live_send_wired=LIVE_SEND_WIRED,
        reasons=reasons,
        keypair_configured=bool(signer.ok),
        keypair_env=ENV_KEYPAIR_PATH,
        limits=limits,
        limits_missing=missing,
        disabled_switch=switch,
        armed_flag=flag,
    )


def can_arm() -> tuple[bool, list[str]]:
    """True when keypair exists, switch is off, and live_armed is set. Limits are locked."""
    st = evaluate()
    return st.armed, list(st.reasons)


def set_limits(
    *,
    max_notional_sol: Any = None,
    max_day_loss_pct: Any = None,
    max_open_mints: Any = None,
    present: Optional[set[str]] = None,
    **_ignored: Any,
) -> LiveStatus:
    """Limits are locked. Writes are ignored; always return authorized caps."""
    return evaluate()


def try_set_disabled(want_disabled: bool) -> tuple[bool, LiveStatus]:
    """Flip the LiveDisabled switch. Turning it OFF requires a local keypair."""
    if want_disabled:
        _overlay.live_disabled = True
        _overlay.live_armed = False
        return True, evaluate()
    signer = inspect_keypair()
    if not signer.ok or not effective_limits().complete():
        return False, evaluate()
    _overlay.live_disabled = False
    return True, evaluate()


def try_set_armed(want_armed: bool) -> tuple[bool, LiveStatus]:
    """Explicit arm flag. Refuses if the switch is on or keypair is missing."""
    if not want_armed:
        _overlay.live_armed = False
        return True, evaluate()
    signer = inspect_keypair()
    switch = disabled_switch()
    if switch or not signer.ok or not effective_limits().complete():
        return False, evaluate()
    _overlay.live_armed = True
    return True, evaluate()
