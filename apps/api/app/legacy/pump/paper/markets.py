"""Read-only markets list for the 市场 table.

Monitored pump.fun paper symbols, with a short in-memory close series for
sparklines. Does not mutate strategy params, journals, or live gates.
"""
from __future__ import annotations

import time
from typing import Any, Optional

from app.legacy.pump.paper.board import _change_pct, _session_candles
from app.legacy.pump.providers.pumpfun_curve_math import market_cap_sol

_SPARK_N = 28


def _spark(candles: list[Any], n: int = _SPARK_N) -> list[float]:
    closes = [float(getattr(c, "c", 0) or 0) for c in candles]
    closes = [c for c in closes if c > 0]
    if len(closes) <= n:
        return closes
    return closes[-n:]


def _curve_extras(provider: Any, symbol: str) -> dict[str, Any]:
    """Market cap and discovery flag without advancing the curve again."""
    curves = getattr(provider, "_curves", None)
    if not isinstance(curves, dict):
        return {}
    lock = getattr(provider, "_lock", None)

    def read() -> dict[str, Any]:
        curve = curves.get(symbol)
        if curve is None:
            return {}
        if getattr(curve, "migrated", False) and int(getattr(curve, "amm_token", 0) or 0) > 0:
            vs = int(curve.amm_sol)
            vt = int(curve.amm_token)
        else:
            vs = int(getattr(curve, "virtual_sol", 0) or 0)
            vt = int(getattr(curve, "virtual_token", 0) or 0)
        cap: Optional[float] = None
        supply = int(getattr(curve, "token_total_supply", 0) or 0)
        if vt > 0 and supply > 0 and vs > 0:
            cap = float(market_cap_sol(vs, vt, supply))
        return {
            "market_cap_sol": cap,
            "discovered": bool(getattr(curve, "discovered", False)),
            "base_name": str(getattr(curve, "base", "") or ""),
        }

    if lock is not None:
        with lock:
            return read()
    return read()


def build_markets() -> dict[str, Any]:
    from app.legacy.pump.discovery import discovery_health_fields
    from app.providers import get_provider
    from app.legacy.pump.strategies.pump_paper_v1 import get_engine

    provider = get_provider()
    rows = get_engine().monitor_rows()
    items: list[dict[str, Any]] = []
    for row in rows:
        symbol = str(row.get("symbol") or "")
        if not symbol:
            continue
        raw_px = row.get("price_sol")
        spot = float(raw_px) if isinstance(raw_px, (int, float)) and raw_px > 0 else None
        candles = _session_candles(provider, symbol)
        price, change = _change_pct(candles, spot)
        extra = _curve_extras(provider, symbol)
        tags = [str(t) for t in (row.get("tags") or [])]
        discovered = bool(extra.get("discovered")) or "discovered" in tags
        buy = float(row.get("buy_notional_1m") or 0.0)
        sell = float(row.get("sell_notional_1m") or 0.0)
        progress = row.get("progress_bps")
        progress_bps = int(progress) if isinstance(progress, (int, float)) else None
        base = str(extra.get("base_name") or "")
        if not base:
            base = symbol.split("/")[0] if "/" in symbol else symbol
        items.append(
            {
                "symbol": symbol,
                "base": base,
                "mint": str(row.get("mint") or ""),
                "price_sol": price,
                "price_usd": None,
                "change_pct": change,
                "volume_sol": buy + sell,
                "market_cap_sol": extra.get("market_cap_sol"),
                "progress_bps": progress_bps,
                "progress_pct": (progress_bps / 100.0) if progress_bps is not None else None,
                "phase": row.get("phase"),
                "discovered": discovered,
                "tags": tags,
                "spark": _spark(candles),
            }
        )

    disc = discovery_health_fields()
    return {
        "mode": "paper",
        "liveEnabled": False,
        "liveDisabled": True,
        "provider": str(getattr(provider, "name", "") or ""),
        "quote": "SOL",
        "usd_available": False,
        "discovery": disc.get("discovery"),
        "discovery_active": disc.get("discoveryActive"),
        "discovery_reason": disc.get("discoveryReason"),
        "asof_ts": int(time.time() * 1000),
        "items": items,
        "empty": len(items) == 0,
    }
