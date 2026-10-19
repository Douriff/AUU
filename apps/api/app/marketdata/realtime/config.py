"""Env-driven settings for the exchange WebSocket market feed (step 1/2 of docs/realtime-ws-plan.md).

Everything is OFF unless ``AUU_RT_WS=on``. With the switch off nothing in this package starts, the
REST ticker cache / 10 s tail / ``/api/v1/ws`` behave exactly as before (see ``app.marketdata.realtime``).
No secrets: only public market-data streams are used.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

_TRUE = {"1", "true", "on", "yes"}
_FALSE = {"0", "false", "off", "no"}

# Binance spot market streams on port 443 (9443 also works). data-stream.binance.vision is the
# market-data-only mirror and is the second choice. Tried in order, rotated after a failed connect.
DEFAULT_BINANCE_WS = ("wss://stream.binance.com:443", "wss://data-stream.binance.vision:443")
# OKX: port 8443 is switched off on 2026-10-31, so no port (443). tickers live on /public, candles on /business.
DEFAULT_OKX_PUBLIC_WS = "wss://ws.okx.com/ws/v5/public"
DEFAULT_OKX_BUSINESS_WS = "wss://ws.okx.com/ws/v5/business"
SUPPORTED = ("binance", "okx")
RT_TFS = ("1h", "4h", "1d")


def _flag(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if raw in _TRUE:
        return True
    if raw in _FALSE:
        return False
    return default


def _num(name: str, default: float, lo: float, hi: float) -> float:
    try:
        val = float(os.getenv(name, "").strip() or default)
    except ValueError:
        val = default
    return max(lo, min(hi, val))


def _urls(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    out = []
    for part in raw.split(","):
        u = part.strip()
        if u.startswith(("wss://", "ws://")) and u not in out:
            out.append(u.rstrip("/"))
    return tuple(out) or default


def rt_enabled() -> bool:
    """Master switch, default off."""
    return _flag("AUU_RT_WS", False)


@dataclass(frozen=True)
class RtConfig:
    enabled: bool = False
    push: bool = True  # forward ticker/kline frames on /api/v1/ws (needs enabled)
    overlay: bool = True  # feed the REST ticker cache / chart tail from the stream while it is fresh
    exchanges: tuple[str, ...] = ("binance", "okx")
    tfs: tuple[str, ...] = RT_TFS
    binance_urls: tuple[str, ...] = DEFAULT_BINANCE_WS
    okx_public_url: str = DEFAULT_OKX_PUBLIC_WS
    okx_business_url: str = DEFAULT_OKX_BUSINESS_WS
    # freshness / liveness
    stale_ms: int = 10_000  # no data for this long -> stream is stale, readers fall back to REST
    idle_sec: float = 20.0  # no message at all for this long -> reconnect (OKX: "ping" first)
    ws_ping_sec: float = 20.0  # protocol-level ping (detects half-open TCP); Binance pings us too
    open_timeout_sec: float = 10.0
    max_conn_age_sec: float = 23.5 * 3600  # Binance cuts every connection at 24 h: rotate a bit earlier
    # reconnect
    backoff_base_sec: float = 1.0
    backoff_max_sec: float = 60.0
    stable_sec: float = 60.0  # a connection that delivered data this long resets the backoff
    max_connects_5min: int = 30  # per feed; Binance allows 300 connection attempts / 5 min / IP
    # REST cross-check
    xcheck_sec: float = 60.0  # 0 = off
    xcheck_bps: float = 50.0
    xcheck_strikes: int = 2
    # /api/v1/ws limits (only applied while the feature is on)
    max_clients: int = 2000
    per_client_key: int = 8  # connections per signed-in account (or IP when anonymous)
    max_subs: int = 64
    msg_burst: int = 20  # inbound client messages per 10 s
    send_timeout_sec: float = 5.0
    client_idle_sec: float = 90.0  # server pings every 15 s; a browser answers with pong


def load_rt_config() -> RtConfig:
    ex_raw = os.getenv("AUU_RT_WS_EXCHANGES", "binance,okx")
    exchanges = tuple(e for e in dict.fromkeys(p.strip().lower() for p in ex_raw.split(",")) if e in SUPPORTED) or ("binance", "okx")
    return RtConfig(
        enabled=rt_enabled(),
        push=_flag("AUU_RT_WS_PUSH", True),
        overlay=_flag("AUU_RT_WS_OVERLAY", True),
        exchanges=exchanges,
        binance_urls=_urls("AUU_RT_BINANCE_WS_URLS", DEFAULT_BINANCE_WS),
        okx_public_url=_urls("AUU_RT_OKX_PUBLIC_WS_URL", (DEFAULT_OKX_PUBLIC_WS,))[0],
        okx_business_url=_urls("AUU_RT_OKX_BUSINESS_WS_URL", (DEFAULT_OKX_BUSINESS_WS,))[0],
        stale_ms=int(_num("AUU_RT_STALE_SEC", 10, 3, 120) * 1000),
        idle_sec=_num("AUU_RT_IDLE_SEC", 20, 2, 120),
        backoff_base_sec=_num("AUU_RT_BACKOFF_BASE_SEC", 1.0, 0.05, 30),
        backoff_max_sec=_num("AUU_RT_BACKOFF_MAX_SEC", 60, 1, 600),
        xcheck_sec=_num("AUU_RT_XCHECK_SEC", 60, 0, 3600),
        xcheck_bps=_num("AUU_RT_XCHECK_BPS", 50, 1, 5000),
        max_clients=int(_num("AUU_RT_WS_MAX_CLIENTS", 2000, 1, 100_000)),
        per_client_key=int(_num("AUU_RT_WS_PER_CLIENT", 8, 1, 1000)),
        max_subs=int(_num("AUU_RT_WS_MAX_SUBS", 64, 1, 1000)),
        msg_burst=int(_num("AUU_RT_WS_MSG_BURST", 20, 2, 1000)),
        send_timeout_sec=_num("AUU_RT_WS_SEND_TIMEOUT_SEC", 5, 0.5, 60),
        client_idle_sec=_num("AUU_RT_WS_CLIENT_IDLE_SEC", 90, 20, 3600),
    )
