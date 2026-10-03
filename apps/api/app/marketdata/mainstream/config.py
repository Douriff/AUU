"""Env-driven settings for the mainstream market-data layer (no secrets here)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field

# Stored timeframes: refreshed in the background, long history kept.
TIMEFRAMES = ("1d", "4h", "1h")
# Intraday timeframes: fetched on demand for the chart, only a recent window kept.
INTRADAY = ("15m", "5m", "1m")
CHART_TFS = ("1m", "5m", "15m", "1h", "4h", "1d")
TF_MS = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
}
DEFAULT_BACKFILL_DAYS = {"1d": 730, "4h": 365, "1h": 90}
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


# Research universe of trend_tsmom_v1 (same list as app.backtest.panel.UNIVERSE_19; kept
# here so the market layer does not import the backtest package).
STRATEGY_UNIVERSE = "BTC ETH SOL XRP DOGE BNB ADA AVAX LINK LTC TRX DOT BCH ETC XLM ATOM FIL UNI NEAR".split()


def _strategy_symbols() -> list[str]:
    raw = os.getenv("AUU_STRATEGY_UNIVERSE", "").strip()
    if not raw:
        return list(STRATEGY_UNIVERSE)
    out: list[str] = []
    for part in raw.split(","):
        base = "".join(ch for ch in part.strip().upper() if ch.isalnum())
        if base and base not in out:
            out.append(base)
    return out[:40]


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
    backfill_days: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_BACKFILL_DAYS))
    intraday_days: int = 7
    intraday_tail_sec: int = 10
    funding_backfill_days: int = 60
    # Strategy universe: daily + 1h candles (1h = hourly risk marks) and funding (no 4h, no live funding), longer funding history.
    strategy_symbols: list[str] = field(default_factory=list)
    strategy_funding_days: int = 730
    refresh: bool = True
    refresh_sec: int = 300
    retries: int = 3
    retry_base_sec: float = 1.0
    timeout_ms: int = 15_000
    max_gap_repairs: int = 5

    def all_symbols(self) -> list[str]:
        """Display symbols first, then strategy-only symbols."""
        return list(self.symbols) + [c for c in self.strategy_symbols if c not in self.symbols]

    def backfill(self, tf: str) -> int:
        return int(self.backfill_days.get(tf, DEFAULT_BACKFILL_DAYS.get(tf, 30)))

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
            "4h": _int("AUU_MAINSTREAM_BACKFILL_4H_DAYS", 365, 1, 1825),
            "1h": _int("AUU_MAINSTREAM_BACKFILL_1H_DAYS", 90, 1, 730),
        },
        intraday_days=_int("AUU_MAINSTREAM_INTRADAY_DAYS", 7, 1, 30),
        intraday_tail_sec=_int("AUU_MAINSTREAM_INTRADAY_TAIL_SEC", 10, 2, 300),
        funding_backfill_days=_int("AUU_MAINSTREAM_FUNDING_DAYS", 60, 1, 730),
        strategy_symbols=_strategy_symbols(),
        strategy_funding_days=_int("AUU_STRATEGY_FUNDING_DAYS", 730, 1, 1825),
        refresh=_flag("AUU_MAINSTREAM_REFRESH", True),
        refresh_sec=_int("AUU_MAINSTREAM_REFRESH_SEC", 300, 30, 86_400),
        retries=_int("AUU_MAINSTREAM_RETRIES", 3, 1, 10),
    )
