"""Read-only Pump.fun new-token discovery (paper path).

Modes: PUMPFUN_DISCOVERY=pumpportal|logs|off
Default: pumpportal if PUMPFUN_PORTAL_API_KEY is set, else off.

Discovery ONLY:
  - normalize WS/RPC create events → NewTokenEvent
  - register mint onto pumpfun_paper watchlist
  - emit WS type=new_token

Portal WS: wss://pumpportal.fun/api/data?api-key=... then subscribeNewToken.
Key load sanitizes quotes/whitespace and keeps the first token only.
HTTP 400/403 → discoveryReason=portal_auth_rejected + exponential backoff (cap ~5min).

Discovery is NOT entry. It never calls RiskGate / PaperBroker / sendTransaction.
No sniper, no subscribeTokenTrade, no Jito, no wallets.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from typing import Any, Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlsplit, urlunparse, urlunsplit

from app.models.contracts import NewTokenEvent
from app.providers import get_provider
from app.providers.pumpfun_curve_math import (
    INITIAL_REAL_TOKEN_RESERVES,
    INITIAL_VIRTUAL_SOL_RESERVES,
    INITIAL_VIRTUAL_TOKEN_RESERVES,
    LAMPORTS_PER_SOL,
    progress_bps,
)
from app.providers.pumpfun_decode import PUMP_PROGRAM_ID, extract_create_from_logs

log = logging.getLogger("auu.discovery")

SOURCES = ("pumpportal", "logs")
DISCOVERY_OPTIONS = ("pumpportal", "logs", "off")
PORTAL_WS_DEFAULT = "wss://pumpportal.fun/api/data"
PORTAL_SUBSCRIBE_METHOD = "subscribeNewToken"
PORTAL_AUTH_REJECT_STATUSES = frozenset({400, 403})
PORTAL_BACKOFF_INITIAL_SEC = 5.0
PORTAL_BACKOFF_CAP_SEC = 300.0  # ~5 min; docs: one WS at a time or IP ban
REASON_PORTAL_AUTH_REJECTED = "portal_auth_rejected"
MAX_DISCOVERED_DEFAULT = 40

_engine: Optional["DiscoveryRuntime"] = None
_multi_key_warned = False


class PortalAuthRejected(Exception):
    """PumpPortal handshake HTTP 400/403 — malformed or invalid/banned key."""

    def __init__(self, status: int) -> None:
        self.status = int(status)
        super().__init__(f"portal HTTP {self.status}")


def sanitize_portal_api_key(raw: Optional[str], *, warn: bool = True) -> str:
    """Return one Portal token. Never concatenate whitespace-separated keys.

    A quoted production .env with two 103-char tokens separated by a space
    yields HTTP 400 (combined) or 403 (each token invalid/expired/banned).
    Quotes/whitespace are stripped. The key itself is never logged.
    """
    global _multi_key_warned
    if raw is None:
        return ""
    s = str(raw).strip().lstrip("\ufeff")
    if len(s) >= 2 and s[0] == s[-1] and s[0] in {'"', "'"}:
        s = s[1:-1].strip()
    parts: list[str] = []
    for tok in s.split():
        t = tok.strip().strip("\"'")
        if t:
            parts.append(t)
    if not parts:
        return ""
    if len(parts) > 1 and warn and not _multi_key_warned:
        _multi_key_warned = True
        log.warning(
            "PUMPFUN_PORTAL_API_KEY has %d whitespace-separated tokens; "
            "using the first and ignoring the rest (never concatenating; key not logged)",
            len(parts),
        )
    return parts[0]


def portal_api_key() -> str:
    return sanitize_portal_api_key(os.getenv("PUMPFUN_PORTAL_API_KEY"))


def build_portal_ws_uri(key: str = "", base: str = "") -> str:
    """Docs URI: wss://pumpportal.fun/api/data?api-key=... (one sanitized token)."""
    uri = (base or "").strip() or PORTAL_WS_DEFAULT
    parts = urlsplit(uri)
    scheme = parts.scheme or "wss"
    netloc = parts.netloc or "pumpportal.fun"
    path = parts.path or "/api/data"
    query_pairs = [
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k.lower() != "api-key"
    ]
    if key:
        query_pairs.append(("api-key", key))
    return urlunsplit((scheme, netloc, path, urlencode(query_pairs), parts.fragment))


def ws_http_status(exc: BaseException) -> Optional[int]:
    """Pull HTTP status from websockets InvalidStatus / InvalidStatusCode."""
    resp = getattr(exc, "response", None)
    for obj in (resp, exc):
        if obj is None:
            continue
        for attr in ("status_code", "status"):
            val = getattr(obj, attr, None)
            if isinstance(val, int) and 100 <= val <= 599:
                return val
    matched = re.search(r"HTTP\s+(\d{3})", str(exc), re.I)
    if matched:
        return int(matched.group(1))
    return None


def next_portal_backoff(current: float) -> tuple[float, float]:
    """Return (sleep_sec, next_current) with exponential growth, cap ~5 min."""
    delay = min(max(float(current), PORTAL_BACKOFF_INITIAL_SEC), PORTAL_BACKOFF_CAP_SEC)
    nxt = min(delay * 2.0, PORTAL_BACKOFF_CAP_SEC)
    return delay, nxt


def should_fallback_to_logs(
    rpc_url: Optional[str] = None,
    fallback_flag: Optional[str] = None,
) -> bool:
    """Auto-fallback to discovery=logs when SOLANA_RPC_URL is set (opt-out via env)."""
    flag = (
        fallback_flag
        if fallback_flag is not None
        else (os.getenv("PUMPFUN_DISCOVERY_FALLBACK") or "")
    ).strip().lower()
    if flag in {"off", "0", "false", "no", "none"}:
        return False
    rpc = (rpc_url if rpc_url is not None else (os.getenv("SOLANA_RPC_URL") or "")).strip()
    return bool(rpc)


def discovery_health_fields() -> dict[str, Any]:
    mode = resolve_discovery_mode()
    engine = _engine
    if engine is None:
        reason = "off" if mode == "off" else "idle"
        active = mode
    else:
        reason = engine.status_reason
        active = engine.mode
    return {
        "discovery": mode,
        "discoveryActive": active,
        "discoveryReason": reason,
        "discoveryOptions": list(DISCOVERY_OPTIONS),
        "portal_key_configured": portal_key_configured(),
    }


def resolve_discovery_mode() -> str:
    raw = (os.getenv("PUMPFUN_DISCOVERY") or "").strip().lower()
    if raw in {"off", "0", "false", "no", "none"}:
        return "off"
    if raw in {"logs", "rpc", "log"}:
        return "logs"
    if raw in {"pumpportal", "portal"}:
        return "pumpportal"
    return "pumpportal" if portal_api_key() else "off"


def portal_key_configured() -> bool:
    return bool(portal_api_key())


def _redact(url: str) -> str:
    return re.sub(r"(?i)api-key=[^&]+", "api-key=***", url)


def _as_int_reserve(val: Any, *, sol_unit: bool = False) -> int:
    if val is None or val == "":
        return 0
    try:
        if isinstance(val, str) and val.strip().isdigit():
            n = int(val.strip())
        else:
            n = float(val)
    except (TypeError, ValueError):
        return 0
    if sol_unit and 0 < n < 1_000_000:
        return int(n * LAMPORTS_PER_SOL)
    if n > 1e18:
        return int(n)
    return int(n)


def _pick(raw: dict[str, Any], *keys: str) -> Any:
    for k in keys:
        if k in raw and raw[k] not in (None, ""):
            return raw[k]
        # case-insensitive
        for rk, rv in raw.items():
            if str(rk).lower() == k.lower() and rv not in (None, ""):
                return rv
    return None


def normalize_new_token(raw: dict[str, Any], source: str) -> Optional[NewTokenEvent]:
    """Map PumpPortal / logs create payloads onto the frozen new_token fields."""
    if source not in SOURCES:
        return None
    mint = str(_pick(raw, "mint", "token", "tokenMint", "ca") or "").strip()
    if not mint or mint.lower() in {"undefined", "null"}:
        return None
    creator = str(
        _pick(raw, "creator", "traderPublicKey", "user", "trader", "bondingCurveCreator") or ""
    ).strip()
    slot_raw = _pick(raw, "slot", "slotIndex")
    try:
        slot = int(slot_raw) if slot_raw is not None else None
    except (TypeError, ValueError):
        slot = None
    ts_raw = _pick(raw, "ts", "timestamp", "blockTime")
    try:
        ts = int(ts_raw) if ts_raw is not None else int(time.time() * 1000)
    except (TypeError, ValueError):
        ts = int(time.time() * 1000)
    if ts < 10_000_000_000:
        ts *= 1000

    vsol = _as_int_reserve(
        _pick(raw, "virtual_sol_reserves", "vSolInBondingCurve", "vSol", "virtualSolReserves"),
        sol_unit=True,
    )
    vtok = _as_int_reserve(
        _pick(raw, "virtual_token_reserves", "vTokensInBondingCurve", "vTokens", "virtualTokenReserves")
    )
    rsol = _as_int_reserve(
        _pick(raw, "real_sol_reserves", "solInBondingCurve", "realSolReserves"),
        sol_unit=True,
    )
    rtok = _as_int_reserve(
        _pick(raw, "real_token_reserves", "realTokenReserves", "tokensInBondingCurve")
    )
    nested = raw.get("initial_reserves") if isinstance(raw.get("initial_reserves"), dict) else None
    if nested:
        vsol = vsol or _as_int_reserve(nested.get("virtual_sol_reserves"), sol_unit=True)
        vtok = vtok or _as_int_reserve(nested.get("virtual_token_reserves"))
        rsol = rsol or _as_int_reserve(nested.get("real_sol_reserves"), sol_unit=True)
        rtok = rtok or _as_int_reserve(nested.get("real_token_reserves"))

    if vsol <= 0:
        vsol = INITIAL_VIRTUAL_SOL_RESERVES
    if vtok <= 0:
        vtok = INITIAL_VIRTUAL_TOKEN_RESERVES
    if rtok <= 0:
        rtok = INITIAL_REAL_TOKEN_RESERVES

    reserves = {
        "virtual_sol_reserves": str(vsol),
        "virtual_token_reserves": str(vtok),
        "real_sol_reserves": str(rsol),
        "real_token_reserves": str(rtok),
    }
    return NewTokenEvent(
        mint=mint,
        creator=creator,
        slot=slot,
        initial_reserves=reserves,
        ts=ts,
        source=source,  # type: ignore[arg-type]
    )


def _ticker_from_raw(raw: dict[str, Any], mint: str) -> str:
    sym = str(_pick(raw, "symbol", "ticker", "base") or "").strip()
    if not sym:
        return f"M{mint[-4:]}"
    return re.sub(r"[^A-Za-z0-9]", "", sym)[:16] or f"M{mint[-4:]}"


def _reserves_ints(ev: NewTokenEvent) -> dict[str, int]:
    ir = ev.initial_reserves or {}
    return {
        "virtual_sol": int(ir.get("virtual_sol_reserves") or 0),
        "virtual_token": int(ir.get("virtual_token_reserves") or 0),
        "real_sol": int(ir.get("real_sol_reserves") or 0),
        "real_token": int(ir.get("real_token_reserves") or 0),
    }


class DiscoveryRuntime:
    """Owns at most one read-only WS. Never submits paper or chain orders."""

    def __init__(self) -> None:
        self.configured_mode = resolve_discovery_mode()
        self.mode = self.configured_mode
        self.status_reason = "off" if self.mode == "off" else "idle"
        self._running = False
        self._seen: set[str] = set()
        self._portal_backoff = PORTAL_BACKOFF_INITIAL_SEC
        self.max_discovered = int(os.getenv("PUMPFUN_DISCOVERY_MAX") or MAX_DISCOVERED_DEFAULT)

    async def ingest(self, raw: dict[str, Any], source: str) -> Optional[NewTokenEvent]:
        ev = normalize_new_token(raw, source)
        if ev is None:
            return None
        if ev.mint in self._seen:
            return None
        self._seen.add(ev.mint)
        if len(self._seen) > 4_000:
            self._seen = set(list(self._seen)[-2_000:])

        provider = get_provider()
        ticker = _ticker_from_raw(raw, ev.mint)
        ints = _reserves_ints(ev)
        bps = progress_bps(ints["real_token"]) if ints["real_token"] else 0
        register = getattr(provider, "register_watch_mint", None)
        if callable(register):
            register(
                ev.mint,
                base=ticker,
                progress_bps=bps,
                reserves=ints if ints["virtual_sol"] and ints["virtual_token"] else None,
                source=ev.source,
                creator=ev.creator,
                max_discovered=self.max_discovered,
            )

        from app.bus import get_hub

        await get_hub().publish({"type": "new_token", "payload": ev.model_dump()})
        return ev

    async def run_loop(self) -> None:
        self._running = True
        if self.mode == "off":
            self.status_reason = "off"
            while self._running:
                await asyncio.sleep(1.0)
            return
        while self._running:
            try:
                if self.mode == "pumpportal":
                    if self.status_reason != REASON_PORTAL_AUTH_REJECTED:
                        self.status_reason = "connecting"
                    await self._run_pumpportal()
                elif self.mode == "logs":
                    if self.status_reason != REASON_PORTAL_AUTH_REJECTED:
                        self.status_reason = "connecting"
                    await self._run_logs()
                else:
                    await asyncio.sleep(1.0)
            except asyncio.CancelledError:
                raise
            except PortalAuthRejected as exc:
                await self._on_portal_auth_rejected(exc.status)
            except Exception:
                log.exception("discovery loop (%s) failed; retry", self.mode)
                await asyncio.sleep(5.0)

    def stop(self) -> None:
        self._running = False

    async def _on_portal_auth_rejected(self, status: int) -> None:
        self.status_reason = REASON_PORTAL_AUTH_REJECTED
        delay, self._portal_backoff = next_portal_backoff(self._portal_backoff)
        log.warning(
            "PumpPortal HTTP %s — %s (malformed/combined key → 400; invalid/expired/banned "
            "key or IP ban → 403). Backing off %.0fs (cap %.0fs). One WS at a time; key not logged.",
            status,
            REASON_PORTAL_AUTH_REJECTED,
            delay,
            PORTAL_BACKOFF_CAP_SEC,
        )
        if should_fallback_to_logs():
            log.warning(
                "auto-fallback to discovery=logs because SOLANA_RPC_URL is set "
                "(avoids Portal retry storm / temporary IP ban)"
            )
            self.mode = "logs"
            return
        await asyncio.sleep(delay)

    async def _run_pumpportal(self) -> None:
        import websockets

        key = portal_api_key()
        extra = (os.getenv("PUMPFUN_PORTAL_WS") or "").strip()
        uri = build_portal_ws_uri(key=key, base=extra)
        log.info("discovery pumpportal connecting %s (key=%s)", _redact(uri), "yes" if key else "no")
        try:
            async with websockets.connect(uri, ping_interval=20, ping_timeout=20) as ws:
                self.status_reason = "subscribed"
                self._portal_backoff = PORTAL_BACKOFF_INITIAL_SEC
                await ws.send(json.dumps({"method": PORTAL_SUBSCRIBE_METHOD}))
                # Intentionally no subscribeTokenTrade / subscribeAccountTrade (metered + sniper-adjacent).
                while self._running:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=60)
                    except asyncio.TimeoutError:
                        continue
                    try:
                        msg = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(msg, dict):
                        continue
                    if msg.get("message") and not msg.get("mint"):
                        continue
                    await self.ingest(msg, "pumpportal")
        except PortalAuthRejected:
            raise
        except Exception as exc:
            status = ws_http_status(exc)
            if status in PORTAL_AUTH_REJECT_STATUSES:
                raise PortalAuthRejected(status) from exc
            raise

    async def _run_logs(self) -> None:
        import websockets

        http = (os.getenv("SOLANA_RPC_URL") or "https://api.mainnet-beta.solana.com").strip()
        parsed = urlparse(http)
        scheme = "wss" if parsed.scheme in {"https", "wss"} else "ws"
        uri = urlunparse((scheme, parsed.netloc, parsed.path or "/", "", parsed.query, ""))
        log.info("discovery logsSubscribe %s mentions=%s", uri, PUMP_PROGRAM_ID)
        async with websockets.connect(uri, ping_interval=20, ping_timeout=20) as ws:
            await ws.send(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "logsSubscribe",
                        "params": [{"mentions": [PUMP_PROGRAM_ID]}, {"commitment": "confirmed"}],
                    }
                )
            )
            while self._running:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=60)
                except asyncio.TimeoutError:
                    continue
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if not isinstance(msg, dict):
                    continue
                params = msg.get("params") or {}
                result = params.get("result") or {}
                value = result.get("value") or {}
                logs = value.get("logs") or []
                if not isinstance(logs, list):
                    continue
                parsed_ev = extract_create_from_logs([str(x) for x in logs])
                if not parsed_ev:
                    continue
                ctx = result.get("context") or {}
                parsed_ev["slot"] = ctx.get("slot")
                parsed_ev["ts"] = int(time.time() * 1000)
                await self.ingest(parsed_ev, "logs")


def get_discovery() -> DiscoveryRuntime:
    global _engine
    if _engine is None:
        _engine = DiscoveryRuntime()
    return _engine


def reset_discovery() -> None:
    global _engine, _multi_key_warned
    _engine = None
    _multi_key_warned = False
