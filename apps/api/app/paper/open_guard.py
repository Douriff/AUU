"""Whether a mint still has paper, live, strategy, or shadow exposure.

Discovery eviction must not drop a curve that one of those still holds.
"""
from __future__ import annotations


def has_open_exposure(symbol: str, mint: str) -> bool:
    sym = (symbol or "").strip()
    mid = (mint or "").strip()
    if not sym and not mid:
        return False

    def _hit(row_symbol: str, row_mint: str | None, qty: float) -> bool:
        if abs(float(qty)) <= 1e-12:
            return False
        if sym and row_symbol == sym:
            return True
        return bool(mid and row_mint and row_mint == mid)

    from app.paper.ledger import get_paper_ledger

    for row_symbol, lots in get_paper_ledger().lots.items():
        for lot in lots:
            if _hit(row_symbol, getattr(lot, "mint", None), lot.qty):
                return True
    from app.live.ledger import get_live_ledger

    for row_symbol, lots in get_live_ledger().lots.items():
        for lot in lots:
            if _hit(row_symbol, getattr(lot, "mint", None), lot.qty):
                return True
    from app.strategies.pump_paper_v1 import get_engine

    for pos in get_engine().positions.values():
        if _hit(pos.symbol, pos.mint, pos.qty):
            return True
    from app.paper.shadow_compare import shadow_open_for

    if shadow_open_for(sym, mid):
        return True
    return False
