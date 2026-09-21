"""LiveDisabled hard gate — fail closed. This PR does not submit chain transactions.

Arming requires ALL of:
  1. live_disabled switch OFF (env AUU_LIVE_DISABLED + settings; default ON)
  2. explicit live_armed=true (env AUU_LIVE_ARMED + settings; default false)
  3. local keypair file at AUU_SOLANA_KEYPAIR_PATH (never a private-key string)
  4. three positive limits: max_notional_sol, max_day_loss, max_open_mints

Missing any of those → reasons LIVE_DISABLED / NO_KEYPAIR / LIMITS_MISSING.
LIVE_SEND_WIRED is False in this scaffold, so health.liveDisabled stays true
even if the arm checklist later passes.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Optional

from app.live.signer import LocalSigner, SignerStatus

ENV_LIVE_DISABLED = "AUU_LIVE_DISABLED"
ENV_LIVE_ARMED = "AUU_LIVE_ARMED"
ENV_KEYPAIR_PATH = "AUU_SOLANA_KEYPAIR_PATH"
ENV_MAX_NOTIONAL_SOL = "AUU_LIVE_MAX_NOTIONAL_SOL"
ENV_MAX_DAY_LOSS = "AUU_LIVE_MAX_DAY_LOSS"
ENV_MAX_OPEN_MINTS = "AUU_LIVE_MAX_OPEN_MINTS"

REASON_LIVE_DISABLED = "LIVE_DISABLED"
REASON_NO_KEYPAIR = "NO_KEYPAIR"
REASON_LIMITS_MISSING = "LIMITS_MISSING"

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


def _env_positive_float(name: str) -> Optional[float]:
    raw = os.getenv(name, "")
    if raw is None or not str(raw).strip():
        return None
    try:
        n = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    return n


def _env_positive_int(name: str) -> Optional[int]:
    n = _env_positive_float(name)
    if n is None:
        return None
    i = int(n)
    if i <= 0:
        return None
    return i


def _positive_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    return n


def _positive_int(value: Any) -> Optional[int]:
    n = _positive_float(value)
    if n is None:
        return None
    i = int(n)
    if i <= 0:
        return None
    return i


@dataclass
class LiveLimits:
    max_notional_sol: Optional[float] = None
    max_day_loss: Optional[float] = None
    max_open_mints: Optional[int] = None

    def missing_names(self) -> list[str]:
        missing: list[str] = []
        if self.max_notional_sol is None or self.max_notional_sol <= 0:
            missing.append("max_notional_sol")
        if self.max_day_loss is None or self.max_day_loss <= 0:
            missing.append("max_day_loss")
        if self.max_open_mints is None or self.max_open_mints <= 0:
            missing.append("max_open_mints")
        return missing

    def complete(self) -> bool:
        return not self.missing_names()

    def as_dict(self) -> dict[str, Optional[float | int]]:
        return {
            "max_notional_sol": self.max_notional_sol,
            "max_day_loss": self.max_day_loss,
            "max_open_mints": self.max_open_mints,
        }


@dataclass
class LiveOverlay:
    """In-process Settings overlay. Env remains the floor for the keypair path."""

    live_disabled: Optional[bool] = None
    live_armed: Optional[bool] = None
    max_notional_sol: Optional[float] = None
    max_day_loss: Optional[float] = None
    max_open_mints: Optional[int] = None
    limits_owned: set[str] = field(default_factory=set)


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
    ov = _overlay
    max_notional = (
        ov.max_notional_sol if "max_notional_sol" in ov.limits_owned else _env_positive_float(ENV_MAX_NOTIONAL_SOL)
    )
    max_day_loss = ov.max_day_loss if "max_day_loss" in ov.limits_owned else _env_positive_float(ENV_MAX_DAY_LOSS)
    max_open = ov.max_open_mints if "max_open_mints" in ov.limits_owned else _env_positive_int(ENV_MAX_OPEN_MINTS)
    return LiveLimits(
        max_notional_sol=max_notional,
        max_day_loss=max_day_loss,
        max_open_mints=max_open,
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
    # Health stays disabled until a later PR wires an official pump-sdk submit.
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
    """True when the checklist is complete (keypair + limits + switch off + armed flag)."""
    st = evaluate()
    return st.armed, list(st.reasons)


def set_limits(
    *,
    max_notional_sol: Any = None,
    max_day_loss: Any = None,
    max_open_mints: Any = None,
    present: Optional[set[str]] = None,
) -> LiveStatus:
    """Save Settings placeholders. Zero/empty stays unset (cannot arm)."""
    mapping = {
        "max_notional_sol": _positive_float(max_notional_sol) if max_notional_sol is not None else None,
        "max_day_loss": _positive_float(max_day_loss) if max_day_loss is not None else None,
        "max_open_mints": _positive_int(max_open_mints) if max_open_mints is not None else None,
    }
    owned = present if present is not None else {k for k, v in mapping.items() if v is not None}
    for key in ("max_notional_sol", "max_day_loss", "max_open_mints"):
        if key not in owned:
            continue
        _overlay.limits_owned.add(key)
        setattr(_overlay, key, mapping[key])
    return evaluate()


def try_set_disabled(want_disabled: bool) -> tuple[bool, LiveStatus]:
    """Flip the LiveDisabled switch. Turning it OFF requires keypair + limits."""
    if want_disabled:
        _overlay.live_disabled = True
        _overlay.live_armed = False
        return True, evaluate()
    signer = inspect_keypair()
    limits = effective_limits()
    if not signer.ok or not limits.complete():
        return False, evaluate()
    _overlay.live_disabled = False
    return True, evaluate()


def try_set_armed(want_armed: bool) -> tuple[bool, LiveStatus]:
    """Explicit arm flag. Refuses if the switch is on, keypair missing, or limits missing."""
    if not want_armed:
        _overlay.live_armed = False
        return True, evaluate()
    signer = inspect_keypair()
    limits = effective_limits()
    switch = disabled_switch()
    if switch or not signer.ok or not limits.complete():
        return False, evaluate()
    _overlay.live_armed = True
    return True, evaluate()
