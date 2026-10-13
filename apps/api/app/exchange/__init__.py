"""User exchange connections (Binance / OKX spot) via ccxt, per-user API keys.

Separate from paper trading and from app.live (liveEnabled is not touched; strategies
never call this). Feature toggle default OFF; admin allowlist; global kill switch.
"""
