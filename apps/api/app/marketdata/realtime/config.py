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
OKX_MODES = ("standby", "full")
# Standby keep-alive subscription on the OKX /public connection: one perpetual's funding rate, pushed every 30-90 s
# (~0.5 KB). It proves the gateway still pushes data, keeps the connection subscribed (OKX drops connections
# without a subscription) and costs almost nothing, unlike ``tickers`` (pushed every 100 ms per coin).
DEFAULT_OKX_PROBE = ("funding-rate", "BTC-USDT-SWAP")
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
    # OKX backup mode (only when Binance is configured too; an OKX-only setup is always full):
    # "standby" (default) = /public stays connected with only the tiny probe subscription, /business (candles) is
    #   not connected; full tickers + candles are subscribed when Binance is disconnected, its data is older than
    #   ``okx_wake_ms``, it is suspect / has a cross-check strike, or REST itself prefers OKX; back to standby
    #   after Binance has been healthy for ``okx_calm_sec`` (doubled per re-activation inside
    #   ``okx_flap_window_sec``, capped at ``okx_calm_max_sec``).
    # "full" = the PR #120 behaviour: every OKX channel subscribed all the time.
    okx_mode: str = "standby"
    okx_wake_ms: int = 3_000
    okx_calm_sec: float = 300.0
    okx_calm_max_sec: float = 3600.0
    okx_flap_window_sec: float = 1800.0
    okx_boot_grace_sec: float = 20.0  # at start-up give Binance this long to deliver before waking OKX
    okx_probe: tuple[str, str] = DEFAULT_OKX_PROBE
    okx_probe_stale_sec: float = 200.0  # standby health: a probe push within this window (pushes every 30-90 s)
    okx_ops_per_hour: int = 400  # OKX allows 480 subscribe/unsubscribe ops per connection per hour
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


def _okx_mode() -> str:
    raw = os.getenv("AUU_RT_OKX_MODE", "").strip().lower()
    return raw if raw in OKX_MODES else "standby"


def _okx_probe() -> tuple[str, str]:
    raw = os.getenv("AUU_RT_OKX_PROBE", "").strip()
    ch, _, inst = raw.partition(":")
    if ch.strip() and inst.strip():
        return ch.strip(), inst.strip().upper()
    return DEFAULT_OKX_PROBE


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
        okx_mode=_okx_mode(),
        okx_wake_ms=int(_num("AUU_RT_OKX_WAKE_SEC", 3, 1, 60) * 1000),
        okx_calm_sec=_num("AUU_RT_OKX_CALM_SEC", 300, 10, 3600),
        okx_calm_max_sec=_num("AUU_RT_OKX_CALM_MAX_SEC", 3600, 10, 86_400),
        okx_flap_window_sec=_num("AUU_RT_OKX_FLAP_WINDOW_SEC", 1800, 60, 86_400),
        okx_boot_grace_sec=_num("AUU_RT_OKX_BOOT_GRACE_SEC", 20, 0, 600),
        okx_probe=_okx_probe(),
        xcheck_sec=_num("AUU_RT_XCHECK_SEC", 60, 0, 3600),
        xcheck_bps=_num("AUU_RT_XCHECK_BPS", 50, 1, 5000),
        max_clients=int(_num("AUU_RT_WS_MAX_CLIENTS", 2000, 1, 100_000)),
        per_client_key=int(_num("AUU_RT_WS_PER_CLIENT", 8, 1, 1000)),
        max_subs=int(_num("AUU_RT_WS_MAX_SUBS", 64, 1, 1000)),
        msg_burst=int(_num("AUU_RT_WS_MSG_BURST", 20, 2, 1000)),
        send_timeout_sec=_num("AUU_RT_WS_SEND_TIMEOUT_SEC", 5, 0.5, 60),
        client_idle_sec=_num("AUU_RT_WS_CLIENT_IDLE_SEC", 90, 20, 3600),
    )
