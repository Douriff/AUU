"""Strategy identity taken from code that is already registered in this repo.

Performance is not filled in here. Definitions are not invented for ids that
this process cannot import.
"""
from __future__ import annotations

from typing import Any, Optional


def _pool() -> dict[str, Any]:
    from app.marketdata.mainstream.config import _strategy_symbols

    symbols = list(_strategy_symbols())
    return {
        "symbols": symbols,
        "size": len(symbols),
        "source": "configured_strategy_universe",
        "coverage": "unknown",
    }


def _signal_family() -> list[dict[str, Any]]:
    from app.strategies import auu_signal_candidates as candidates
    from app.strategies.auu_signal_v0 import PARAMS, VERSION

    pool = _pool()
    v0_weights = dict(PARAMS["weights"])
    rows = [
        {
            "id": VERSION,
            "version": VERSION,
            "version_kind": "formula_version",
            "style": "momentum_volume_funding",
            "direction": "long_only" if PARAMS.get("long_only") else "long_short",
            "timeframe": "1d",
            "pool": pool,
            "hold": {
                "kind": "hysteresis",
                "enter": float(PARAMS["enter_th"]),
                "exit": float(PARAMS["exit_th"]),
                "fill": "next_bar_open",
            },
            "weights": v0_weights,
            "long_only": bool(PARAMS.get("long_only")),
            "tag_abs": float(PARAMS["tag_abs"]),
            "primary_book": "shadow",
            "registered_at": candidates.REGISTERED_AT,
            "records": "hypothetical_forward_shadow",
            "orders": False,
            "params_hash": None,
        }
    ]
    for cid, spec in candidates.CANDIDATES.items():
        weights = dict(spec["weights"])
        style = "momentum_volume_funding" if "funding" in weights else "momentum_volume"
        rows.append(
            {
                "id": cid,
                "version": candidates.PARAMS_HASH[cid],
                "version_kind": "params_sha256_16",
                "style": style,
                "direction": "long_only" if spec["long_only"] else "long_short",
                "timeframe": spec["tf"],
                "pool": pool,
                "hold": {
                    "kind": "hysteresis",
                    "enter": float(spec["enter_th"]),
                    "exit": float(spec["exit_th"]),
                    "fill": "next_bar_open",
                },
                "weights": weights,
                "long_only": bool(spec["long_only"]),
                "tag_abs": float(PARAMS["tag_abs"]),
                "primary_book": "shadow",
                "registered_at": candidates.REGISTERED_AT,
                "records": "hypothetical_forward_shadow",
                "orders": False,
                "params_hash": candidates.PARAMS_HASH[cid],
            }
        )
    return rows


def _tsmom() -> dict[str, Any]:
    from app.strategies.trend_tsmom import TrendTSMOM

    described = TrendTSMOM().describe()
    return {
        "id": "trend_tsmom_v1",
        "version": "trend_tsmom_v1",
        "version_kind": "code_default",
        "style": "time_series_momentum",
        "direction": "long_only" if described.get("long_only") else "long_short",
        "timeframe": "1d",
        "pool": _pool(),
        "hold": {"kind": "daily_rebalance", "enter": None, "exit": None, "fill": "close"},
        "weights": None,
        "long_only": bool(described.get("long_only")),
        "tag_abs": None,
        "primary_book": "paper",
        "registered_at": None,
        "records": "system_paper",
        "orders": False,
        "params_hash": None,
        "code_params": {
            "lookbacks": list(described.get("lookbacks") or []),
            "target_vol": described.get("target_vol"),
            "vol_window": described.get("vol_window"),
        },
    }


def _s3() -> dict[str, Any]:
    from app.paper.shadow_s3 import RULE, RULE_HASH

    return {
        "id": "shadow_s3",
        "version": RULE_HASH,
        "version_kind": "rule_sha256_16",
        "style": "funding_extreme_long",
        "direction": "long_only",
        "timeframe": "1h",
        "pool": {"symbols": None, "size": None, "source": "funding_settlements", "coverage": "unknown"},
        "hold": {"kind": "fixed_hours", "hours": int(RULE["hold_hours"]), "enter": None, "exit": None, "fill": "bar_close"},
        "weights": None,
        "long_only": True,
        "tag_abs": None,
        "primary_book": "shadow",
        "registered_at": None,
        "records": "hypothetical_shadow",
        "orders": False,
        "params_hash": RULE_HASH,
        "rule_id": RULE["id"],
    }


def _h2() -> dict[str, Any]:
    from app.paper.shadow_h2 import FROZEN_SHA256, PARAMS

    trend = PARAMS["trend"]
    return {
        "id": "shadow_h2",
        "version": FROZEN_SHA256,
        "version_kind": "frozen_sha256",
        "style": "trend_carry_idle",
        "direction": "mixed",
        "timeframe": "1d",
        "pool": {
            "symbols": list(trend["universe"]),
            "size": len(trend["universe"]),
            "source": "frozen_h2_params",
            "coverage": "unknown",
        },
        "hold": {"kind": "sleeve_daily", "enter": None, "exit": None, "fill": "close"},
        "weights": None,
        "long_only": False,
        "tag_abs": None,
        "primary_book": "shadow",
        "registered_at": str(PARAMS["start"].get("inception_day") or ""),
        "records": "hypothetical_shadow",
        "orders": False,
        "params_hash": FROZEN_SHA256,
        "accounting_unit": float(PARAMS["start"]["nav"]),
    }


def identities() -> list[dict[str, Any]]:
    return [*_signal_family(), _tsmom(), _s3(), _h2()]


def identity(strategy_id: str) -> Optional[dict[str, Any]]:
    for row in identities():
        if row["id"] == strategy_id:
            return row
    return None
