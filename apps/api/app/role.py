"""Node role: ``AUU_ROLE=primary`` (default) or ``standby``.

A standby node (e.g. the Hong Kong disaster-recovery host) serves the site and
keeps live market data fresh, but must never write the shared ledgers or send
mail while the primary is alive, otherwise two hosts would rebalance, the
ledgers would fork and every digest would arrive twice. On a standby:

* the daily strategy runner (and with it the exec-price shadow and the risk
  caps), the S3 and H2 shadow records, the alert/digest mailer, the cross-source
  recon and the uptime recorder do not start, and their ``*_enabled()`` checks
  report off, whatever their own ``AUU_*`` switch says;
* writes under the ledger-owning API prefixes answer ``503 STANDBY``.

Market data refresh, news, login and the read-only pages keep working. Any
value other than empty/``primary`` counts as standby (fail safe: a typo never
starts a second trading node). Failover = set ``AUU_ROLE=primary`` and restart.
"""
from __future__ import annotations

import os

PRIMARY = "primary"
STANDBY = "standby"

# Background jobs a standby never runs (reported in health).
STANDBY_OFF = ("strategy", "exec_shadow", "shadow_s3", "shadow_h2", "alerts", "recon", "uptime")

# API prefixes whose writes change a ledger; refused on a standby.
STANDBY_WRITE_BLOCK_PREFIXES = (
    "/api/v1/mainstream/",
    "/api/v1/strategy/",
    "/api/v1/paper/",
    "/api/v1/pipeline/",
    "/api/v1/risk/",
    "/api/v1/live/",
    "/api/v1/watch/",
    "/api/v1/trade/",
)

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def node_role() -> str:
    raw = (os.getenv("AUU_ROLE") or "").strip().lower()
    return PRIMARY if raw in {"", PRIMARY} else STANDBY


def is_standby() -> bool:
    return node_role() == STANDBY


def standby_blocks(method: str, path: str) -> bool:
    """True when a standby must refuse this request (ledger-changing write)."""
    if not is_standby() or method.upper() in _SAFE_METHODS:
        return False
    return path.startswith(STANDBY_WRITE_BLOCK_PREFIXES)


def role_status() -> dict:
    standby = is_standby()
    return {"role": node_role(), "standby": standby, "jobsOff": list(STANDBY_OFF) if standby else []}


__all__ = ["PRIMARY", "STANDBY", "STANDBY_OFF", "node_role", "is_standby", "standby_blocks", "role_status"]
