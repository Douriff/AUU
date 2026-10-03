"""Mainstream strategies (pump_paper_v1 lives in app.legacy.pump).

Strategies here are shared by the backtest engine (``app.backtest``) and the paper runner:
``targets(panel)`` -> per-day target weights (causal), ``decide(panel)`` -> newest row.
"""
from app.strategies.trend_tsmom import TrendTSMOM, TrendTSMOMParams

REGISTRY = {"trend_tsmom_v1": TrendTSMOM}

__all__ = ["REGISTRY", "TrendTSMOM", "TrendTSMOMParams"]
