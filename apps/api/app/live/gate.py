"""LiveDisabled hard gate — fail closed. This PR does not submit chain transactions.

LiveLimits is a separate type from paper RiskLimits (do not reuse paper caps):
  max_notional_sol = 1.0
  max_day_loss_pct = 0.045
  max_open_mints = 10

Reject with LIVE_DISABLED (also RiskOut.tags) unless ALL of:
  1. liveEnabled true (default false)
  2. user explicit secondary confirm (liveConfirmed)
  3. local keypair mounted at AUU_SOLANA_KEYPAIR_PATH (never a private-key string)
  4. LiveLimits present (locked caps above)

The send gate lives in app.live.send and is independent of the liveEnabled
switch. LIVE_SEND_WIRED is False, so the default runtime sends zero chain txs
even if the checklist later passes.

Arming also requires an explicit account, currency, notional quota, leverage,
stop-loss and loss cap. None of those has a default. Unset, out of range, or
above the leverage ceiling stays disarmed. This module does not choose those
values and does not arm itself.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from app.live.signer import LocalSigner, SignerStatus

ENV_LIVE_ENABLED = "AUU_LIVE_ENABLED"
ENV_LIVE_DISABLED = "AUU_LIVE_DISABLED"
ENV_LIVE_CONFIRMED = "AUU_LIVE_CONFIRMED"
ENV_LIVE_ARMED = "AUU_LIVE_ARMED"
ENV_KEYPAIR_PATH = "AUU_SOLANA_KEYPAIR_PATH"
# LOCAL-ONLY default. Gitignored. Never commit this file. Override with env.
DEFAULT_KEYPAIR_RELPATH = "secrets/live-keypair.json"
ENV_MAX_NOTIONAL_SOL = "AUU_LIVE_MAX_NOTIONAL_SOL"
ENV_MAX_DAY_LOSS_PCT = "AUU_LIVE_MAX_DAY_LOSS_PCT"
ENV_MAX_OPEN_MINTS = "AUU_LIVE_MAX_OPEN_MINTS"
# Explicit arm confirmation. No defaults: unset means the checklist is incomplete.
ENV_LIVE_ACCOUNT = "AUU_LIVE_ACCOUNT"
ENV_LIVE_CURRENCY = "AUU_LIVE_CURRENCY"
ENV_LIVE_QUOTA = "AUU_LIVE_MAX_NOTIONAL"
ENV_LIVE_LEVERAGE = "AUU_LIVE_LEVERAGE"
ENV_LIVE_STOP = "AUU_LIVE_STOP_LOSS"
ENV_LIVE_MAX_LOSS = "AUU_LIVE_MAX_LOSS"

REASON_LIVE_DISABLED = "LIVE_DISABLED"
REASON_NO_KEYPAIR = "NO_KEYPAIR"
REASON_LIMITS_MISSING = "LIMITS_MISSING"
REASON_ARM_INCOMPLETE = "LIVE_ARM_INCOMPLETE"
# Ceiling only. A missing leverage is incomplete; it is never filled in with 1.
LEVERAGE_CEILING = 5.0

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
    """Live-only caps. Never read paper RiskLimits / pump-paper-v1 params."""

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

    live_enabled: Optional[bool] = None
    live_confirmed: Optional[bool] = None


@dataclass
class LiveStatus:
    live_enabled: bool
    live_confirmed: bool
    live_disabled: bool
    live_armed: bool
    live_send_wired: bool
    reasons: list[str]
    keypair_configured: bool
    keypair_env: str
    pubkey: str
    limits: LiveLimits
    limits_missing: list[str]
    disabled_switch: bool
    armed_flag: bool

    @property
    def armed(self) -> bool:
        """True only when every arm condition holds (still no chain submit here)."""
        return not self.reasons

    @property
    def checklist_ok(self) -> bool:
        return self.armed

    def primary_reason(self) -> str:
        for code in (REASON_LIVE_DISABLED, REASON_NO_KEYPAIR, REASON_LIMITS_MISSING, REASON_ARM_INCOMPLETE):
            if code in self.reasons:
                return code
        return REASON_LIVE_DISABLED

    def as_dict(self) -> dict[str, Any]:
        return {
            "liveEnabled": self.live_enabled,
            "liveConfirmed": self.live_confirmed,
            "liveDisabled": self.live_disabled,
            "liveArmed": self.live_armed,
            "liveSendWired": self.live_send_wired,
            "reasons": list(self.reasons),
            "keypairConfigured": self.keypair_configured,
            "keypairMounted": bool(self.keypair_configured),
            "pubkey": self.pubkey or None,
            "keypairEnv": self.keypair_env,
            "keypairRelpath": DEFAULT_KEYPAIR_RELPATH,
            "keypairPathHint": (
                "LOCAL-ONLY gitignored file secrets/live-keypair.json "
                "(or AUU_SOLANA_KEYPAIR_PATH on this machine). "
                "Never paste a secret; never commit or upload the file"
            ),
            "limits": self.limits.as_dict(),
            "limitsLocked": True,
            "limitsMissing": list(self.limits_missing),
            "disabledSwitch": self.disabled_switch,
            "armedFlag": self.armed_flag,
            "venue": "live",
            "sendEnabled": False,
            "armConfirmation": _arm_public(),
        }


_overlay = LiveOverlay()
_signer = LocalSigner()


def get_overlay() -> LiveOverlay:
    return _overlay


def reset_live_state() -> None:
    global _overlay, _signer
    _overlay = LiveOverlay()
    _signer = LocalSigner()


def _repo_root() -> Path:
    # apps/api/app/live/gate.py → repo root
    return Path(__file__).resolve().parents[4]


def keypair_path() -> str:
    """Resolved local path. Never returned on health — use DEFAULT_KEYPAIR_RELPATH.

    Relative paths (including secrets/live-keypair.json) are tried against the
    repo root and the process cwd so a local mount is found from apps/api.
    """
    raw = (os.getenv(ENV_KEYPAIR_PATH) or "").strip() or DEFAULT_KEYPAIR_RELPATH
    p = Path(raw).expanduser()
    if p.is_file():
        return str(p)
    if not p.is_absolute():
        for base in (_repo_root(), Path.cwd(), Path.cwd().parent):
            cand = base / p
            try:
                if cand.is_file():
                    return str(cand)
            except OSError:
                continue
    return str((_repo_root() / DEFAULT_KEYPAIR_RELPATH) if raw == DEFAULT_KEYPAIR_RELPATH else p)


def inspect_keypair() -> SignerStatus:
    return _signer.inspect(keypair_path() or None)


def _explicit_number(name: str, *, low: float, high: float, low_inclusive: bool) -> Optional[float]:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return None
    number = _parse_positive_float(raw)
    if number is None:
        return None
    if low_inclusive:
        ok = low <= number <= high
    else:
        ok = low < number <= high
    return number if ok else None


def arm_confirmation() -> dict[str, Any]:
    """Operator-supplied arm fields. Missing or rejected values are not replaced."""
    missing: list[str] = []
    account = (os.getenv(ENV_LIVE_ACCOUNT) or "").strip()
    account_set = bool(account) and len(account) <= 64 and "\n" not in account and "\r" not in account
    if not account_set:
        missing.append("account")
    currency = (os.getenv(ENV_LIVE_CURRENCY) or "").strip().upper()
    if not (2 <= len(currency) <= 12 and currency.isalpha()):
        missing.append("currency")
        currency = ""
    quota = _explicit_number(ENV_LIVE_QUOTA, low=0.0, high=1_000_000_000.0, low_inclusive=False)
    if quota is None:
        missing.append("max_notional")
    leverage = _explicit_number(ENV_LIVE_LEVERAGE, low=1.0, high=LEVERAGE_CEILING, low_inclusive=True)
    if leverage is None:
        missing.append("leverage")
    stop = _explicit_number(ENV_LIVE_STOP, low=0.0, high=1.0, low_inclusive=False)
    if stop is None:
        missing.append("stop_loss")
    loss = _explicit_number(ENV_LIVE_MAX_LOSS, low=0.0, high=1.0, low_inclusive=False)
    if loss is None:
        missing.append("max_loss")
    return {
        "complete": not missing,
        "missing": missing,
        "accountSet": account_set,
        "currency": currency or None,
        "maxNotional": quota,
        "leverage": leverage,
        "stopLoss": stop,
        "maxLoss": loss,
    }


def _arm_public() -> dict[str, Any]:
    arm = arm_confirmation()
    return {key: arm[key] for key in ("complete", "missing", "accountSet", "currency", "maxNotional", "leverage", "stopLoss", "maxLoss")}


def effective_limits() -> LiveLimits:
    """Locked caps; env may only tighten. Zero/unset env falls back to locked values."""
    return LiveLimits(
        max_notional_sol=clamp_live_notional(_env_positive_float(ENV_MAX_NOTIONAL_SOL)),
        max_day_loss_pct=clamp_live_day_loss_pct(_env_positive_float(ENV_MAX_DAY_LOSS_PCT)),
        max_open_mints=clamp_live_open_mints(_env_positive_int(ENV_MAX_OPEN_MINTS)),
    )


def live_enabled() -> bool:
    """User switch. Default false. AUU_LIVE_DISABLED remains an inverse alias."""
    if _overlay.live_enabled is not None:
        return bool(_overlay.live_enabled)
    if os.getenv(ENV_LIVE_ENABLED) is not None and str(os.getenv(ENV_LIVE_ENABLED)).strip():
        return _env_bool(ENV_LIVE_ENABLED, False)
    if os.getenv(ENV_LIVE_DISABLED) is not None and str(os.getenv(ENV_LIVE_DISABLED)).strip():
        return not _env_bool(ENV_LIVE_DISABLED, True)
    return False


def live_confirmed() -> bool:
    """Explicit secondary confirm. Default false. AUU_LIVE_ARMED is an alias."""
    if _overlay.live_confirmed is not None:
        return bool(_overlay.live_confirmed)
    if os.getenv(ENV_LIVE_CONFIRMED) is not None and str(os.getenv(ENV_LIVE_CONFIRMED)).strip():
        return _env_bool(ENV_LIVE_CONFIRMED, False)
    if os.getenv(ENV_LIVE_ARMED) is not None and str(os.getenv(ENV_LIVE_ARMED)).strip():
        return _env_bool(ENV_LIVE_ARMED, False)
    return False


def disabled_switch() -> bool:
    """Inverse of liveEnabled (compat for older liveDisabled clients)."""
    return not live_enabled()


def armed_flag() -> bool:
    """Secondary confirm flag (compat: live_armed)."""
    return live_confirmed()


def evaluate() -> LiveStatus:
    limits = effective_limits()
    missing = limits.missing_names()
    signer = inspect_keypair()
    enabled = live_enabled()
    confirmed = live_confirmed()
    keypair_ok = bool(signer.ok)
    limits_ok = not missing
    arm_ok = bool(arm_confirmation()["complete"])
    checklist = enabled and confirmed and keypair_ok and limits_ok and arm_ok
    reasons: list[str] = []
    if not checklist:
        reasons.append(REASON_LIVE_DISABLED)
    if not keypair_ok:
        reasons.append(REASON_NO_KEYPAIR)
    if missing:
        reasons.append(REASON_LIMITS_MISSING)
    if not arm_ok:
        reasons.append(REASON_ARM_INCOMPLETE)
    live_disabled = (not checklist) or (not LIVE_SEND_WIRED)
    return LiveStatus(
        live_enabled=enabled,
        live_confirmed=confirmed,
        live_disabled=live_disabled,
        live_armed=checklist,
        live_send_wired=LIVE_SEND_WIRED,
        reasons=reasons,
        keypair_configured=keypair_ok,
        keypair_env=ENV_KEYPAIR_PATH,
        pubkey=signer.pubkey if keypair_ok else "",
        limits=limits,
        limits_missing=missing,
        disabled_switch=not enabled,
        armed_flag=confirmed,
    )


def can_arm() -> tuple[bool, list[str]]:
    """True when keypair exists, liveEnabled, confirmed, and limits present."""
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
    return try_set_enabled(not want_disabled, confirmed=False)


def try_set_enabled(want_enabled: bool, *, confirmed: bool = False) -> tuple[bool, LiveStatus]:
    """liveEnabled switch. Enabling without secondary confirm still stays LIVE_DISABLED."""
    if not want_enabled:
        _overlay.live_enabled = False
        _overlay.live_confirmed = False
        return True, evaluate()
    signer = inspect_keypair()
    if not signer.ok or not effective_limits().complete():
        return False, evaluate()
    _overlay.live_enabled = True
    if confirmed:
        _overlay.live_confirmed = True
    return True, evaluate()


def try_set_armed(want_armed: bool) -> tuple[bool, LiveStatus]:
    """Explicit secondary confirm. Refuses if liveEnabled is off or keypair is missing."""
    if not want_armed:
        _overlay.live_confirmed = False
        return True, evaluate()
    signer = inspect_keypair()
    if (not live_enabled()) or (not signer.ok) or (not effective_limits().complete()) or (not arm_confirmation()["complete"]):
        return False, evaluate()
    _overlay.live_confirmed = True
    return True, evaluate()


def try_set_enabled_with_confirm(want_enabled: bool, *, confirmed: bool) -> tuple[bool, LiveStatus]:
    """Settings path: liveEnabled + secondary confirm in one shot."""
    if not want_enabled:
        return try_set_enabled(False)
    if not confirmed:
        return False, evaluate()
    if not arm_confirmation()["complete"]:
        return False, evaluate()
    ok_en, st = try_set_enabled(True, confirmed=False)
    if not ok_en:
        return False, st
    return try_set_armed(True)
