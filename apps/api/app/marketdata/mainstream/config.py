"""Env-driven settings for the mainstream market-data layer (no secrets here)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field

TIMEFRAMES = ("1d", "1h")
TF_MS = {"1h": 3_600_000, "1d": 86_400_000}
FUNDING_STEP_MS = 8 * 3_600_000
SUPPORTED_EXCHANGES = ("binance", "okx")

_TRUE = {"1", "true", "on", "yes"}
_FALSE = {"0", "false", "off", "no"}


def _flag(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if raw in _TRUE:
        return True
    if raw in _FALSE:
        return False
    return default


def _int(name: str, default: int, lo: int, hi: int) -> int:
    try:
        val = int(os.getenv(name, "").strip() or default)
    except ValueError:
        val = default
    return max(lo, min(hi, val))


def _symbols() -> list[str]:
    raw = os.getenv("AUU_MAINSTREAM_SYMBOLS", "BTC,ETH,SOL")
    out: list[str] = []
    for part in raw.split(","):
        base = "".join(ch for ch in part.strip().upper() if ch.isalnum())
        if base and base not in out:
            out.append(base)
    return out[:20] or ["BTC", "ETH", "SOL"]


def _exchanges() -> list[str]:
    raw = os.getenv("AUU_MAINSTREAM_EXCHANGE", "auto").strip().lower()
    if raw in SUPPORTED_EXCHANGES:
        return [raw]
    # auto (or comma list): try in order, fall back when one is blocked/down.
    order = [p.strip() for p in raw.split(",") if p.strip() in SUPPORTED_EXCHANGES]
    return order or ["binance", "okx"]


@dataclass
class MainstreamConfig:
    enabled: bool = True
    symbols: list[str] = field(default_factory=lambda: ["BTC", "ETH", "SOL"])
    quote: str = "USDT"
    exchanges: list[str] = field(default_factory=lambda: ["binance", "okx"])
    backfill_days: dict[str, int] = field(default_factory=lambda: {"1d": 730, "1h": 90})
    funding_backfill_days: int = 60
    refresh: bool = True
    refresh_sec: int = 300
    retries: int = 3
    retry_base_sec: float = 1.0
    timeout_ms: int = 15_000
    max_gap_repairs: int = 5

    def spot(self, base: str) -> str:
        return f"{base}/{self.quote}"

    def perp(self, base: str) -> str:
        return f"{base}/{self.quote}:{self.quote}"


def load_config() -> MainstreamConfig:
    quote = "".join(ch for ch in os.getenv("AUU_MAINSTREAM_QUOTE", "USDT").upper() if ch.isalnum()) or "USDT"
    return MainstreamConfig(
        enabled=_flag("AUU_MAINSTREAM_DATA", True),
        symbols=_symbols(),
        quote=quote,
        exchanges=_exchanges(),
        backfill_days={
            "1d": _int("AUU_MAINSTREAM_BACKFILL_1D_DAYS", 730, 1, 3650),
            "1h": _int("AUU_MAINSTREAM_BACKFILL_1H_DAYS", 90, 1, 730),
        },
        funding_backfill_days=_int("AUU_MAINSTREAM_FUNDING_DAYS", 60, 1, 730),
        refresh=_flag("AUU_MAINSTREAM_REFRESH", True),
        refresh_sec=_int("AUU_MAINSTREAM_REFRESH_SEC", 300, 30, 86_400),
        retries=_int("AUU_MAINSTREAM_RETRIES", 3, 1, 10),
    )
