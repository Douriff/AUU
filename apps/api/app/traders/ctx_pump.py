"""progress_bps / phase for observed mints — P0 authority is in-process ctx.pump.

Third-party indexers are not used here. Lookup is local paper curve (pumpfun_paper)
or an injected map. Never hits Pump frontend HTTP.
"""
from __future__ import annotations

from typing import Any, Optional


def _phase_from_snap(snap: Any) -> str:
    migrated = bool(getattr(snap, "migrated", False))
    complete = bool(getattr(snap, "complete", False))
    phase = str(getattr(snap, "phase", "") or "")
    if migrated:
        return "amm"
    if complete:
        return "graduating"
    if phase in {"curve", "graduating", "amm", "unknown"}:
        return phase
    return "curve"


def lookup_ctx_pump(mint: str, pump_by_mint: Optional[dict[str, dict[str, Any]]] = None) -> Optional[dict[str, Any]]:
    """Return {progress_bps, phase, price_sol?, symbol?} for a mint, or None."""
    if pump_by_mint and mint in pump_by_mint:
        row = pump_by_mint[mint]
        return {
            "progress_bps": row.get("progress_bps"),
            "phase": row.get("phase") or "unknown",
            "price_sol": row.get("price_sol"),
            "symbol": row.get("symbol"),
        }
    try:
        from app.providers import get_provider

        provider = get_provider()
    except Exception:
        return None
    snap = None
    get_snap = getattr(provider, "get_pumpfun_snapshot", None)
    if callable(get_snap):
        snap = get_snap(mint)
        if snap is None:
            try:
                for info in provider.list_symbols():
                    info_mint = getattr(info, "mint", None)
                    if info_mint == mint or info.symbol == mint:
                        snap = get_snap(info.symbol)
                        if snap is not None:
                            break
            except Exception:
                snap = None
    if snap is None:
        return None
    return {
        "progress_bps": int(getattr(snap, "progress_bps", 0) or 0),
        "phase": _phase_from_snap(snap),
        "price_sol": float(getattr(snap, "price_sol", 0.0) or 0.0),
        "symbol": getattr(snap, "symbol", None),
    }


def overlay_progress(
    mint: str,
    *,
    progress_bps: Optional[int] = None,
    phase: Optional[str] = None,
    pump_by_mint: Optional[dict[str, dict[str, Any]]] = None,
) -> tuple[Optional[int], str]:
    """Positions use current ctx.pump. Historical fills keep their own progress if set."""
    found = lookup_ctx_pump(mint, pump_by_mint)
    if found is None:
        return progress_bps, phase or "unknown"
    bps = found.get("progress_bps")
    ph = str(found.get("phase") or phase or "unknown")
    return (int(bps) if bps is not None else progress_bps), ph
