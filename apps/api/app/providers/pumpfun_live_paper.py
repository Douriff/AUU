"""Real-market paper provider.

Prices, prints, and reserves come from observed chain trades. No seeded RNG,
no interpolated ticks, and no transactions. ``liveEnabled`` is unchanged
(the live adapter stays dark). PumpPortal ``subscribeTokenTrade`` is preferred;
Solana ``logsSubscribe`` plus TradeEvent decode is the fallback.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Any, AsyncIterator, Optional
from urllib.parse import urlparse, urlunparse

from app.models.contracts import (
    Candle,
    Fill,
    PumpfunPaperSnapshot,
    PumpfunTradeTick,
    RiskEvent,
    SignalEvent,
    SymbolInfo,
)
from app.providers.base import MarketDataProvider
from app.providers.pumpfun_curve_math import (
    INITIAL_REAL_TOKEN_RESERVES,
    INITIAL_VIRTUAL_SOL_RESERVES,
    INITIAL_VIRTUAL_TOKEN_RESERVES,
    LAMPORTS_PER_SOL,
    TOKEN_TOTAL_SUPPLY,
    market_cap_sol,
    price_sol,
    price_sol_str,
    progress_bps,
)
from app.providers.pumpfun_decode import PUMP_PROGRAM_ID, extract_trade_from_logs

log = logging.getLogger("auu.pumpfun_live_paper")

INTERVAL_MS = {
    "1s": 1_000,
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "1h": 3_600_000,
}
_PUMP_POOLS = {"pump", "pumpfun", "pump.fun", ""}
_TRADE_CAP = 80


def feed_enabled() -> bool:
    raw = os.getenv("AUU_LIVE_PAPER_FEED", "on").strip().lower()
    return raw not in {"0", "off", "false", "no"}


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
    return int(n)


def _demo_mint(mint: str) -> bool:
    return (mint or "").startswith("DemoMint")


@dataclass
class _Curve:
    mint: str
    symbol: str
    base: str
    virtual_sol: int
    virtual_token: int
    real_sol: int
    real_token: int
    complete: bool = False
    discovered: bool = False
    source: str = ""
    creator: str = ""
    updated_ts: int = 0
    token_total_supply: int = TOKEN_TOTAL_SUPPLY


class PumpfunLivePaperProvider(MarketDataProvider):
    """Watch-list of real bonding curves. Prints only when a trade is observed."""

    name = "pumpfun_live_paper"

    def __init__(self, watch_mints: str | None = None):
        self._lock = threading.RLock()
        self._curves: dict[str, _Curve] = {}
        self._by_mint: dict[str, str] = {}
        self._trades: dict[str, list[dict]] = {}
        self._candles: dict[str, dict[str, list[Candle]]] = {}
        self._feed_running = False
        raw = watch_mints if watch_mints is not None else os.getenv("PUMPFUN_WATCH_MINTS", "")
        for part in (raw or "").split(","):
            item = part.strip()
            if not item:
                continue
            bits = [b.strip() for b in item.split(":") if b.strip()]
            mint = bits[0]
            base = bits[1].replace("/SOL", "") if len(bits) > 1 else ""
            self.register_watch_mint(mint, base=base or None, source="watch")

    def list_symbols(self) -> list[SymbolInfo]:
        with self._lock:
            return [
                SymbolInfo(symbol=c.symbol, base=c.base, quote="SOL", kind="pumpfun_curve", mint=c.mint)
                for c in self._curves.values()
            ]

    def get_candles(
        self, symbol: str, interval: str = "1m", from_ts: int | None = None, to_ts: int | None = None
    ) -> list[Candle]:
        iv = interval if interval in INTERVAL_MS else "1m"
        with self._lock:
            out = list(self._candles.get(symbol, {}).get(iv, []))
        if from_ts is not None:
            out = [c for c in out if c.t >= from_ts]
        if to_ts is not None:
            out = [c for c in out if c.t <= to_ts]
        return out

    def get_signals(
        self, symbol: str, from_ts: int | None = None, to_ts: int | None = None
    ) -> list[SignalEvent]:
        return []

    def get_fills(
        self, symbol: str, from_ts: int | None = None, to_ts: int | None = None
    ) -> list[Fill]:
        return []

    def get_risk_events(self, symbol: str) -> list[RiskEvent]:
        return []

    def watch_flags(self, symbol: str) -> dict:
        with self._lock:
            c = self._curves.get(symbol)
            if c is None:
                return {}
            return {"discovered": bool(c.discovered), "source": c.source, "creator": c.creator}

    def register_watch_mint(
        self,
        mint: str,
        *,
        base: str | None = None,
        progress_bps: int = 0,
        reserves: dict[str, int] | None = None,
        source: str = "",
        creator: str = "",
        max_discovered: int = 40,
        complete: bool = False,
    ):
        """Add a non-graduated pump mint. Demo mints and complete curves are ignored."""
        mint = (mint or "").strip()
        if not mint or _demo_mint(mint) or complete or int(progress_bps) >= 10_000:
            return None
        with self._lock:
            existing = self._by_mint.get(mint)
            if existing:
                return self._curves.get(existing)
            if reserves and int(reserves.get("real_token") or 0) < 0:
                return None
            discovered = [c for c in self._curves.values() if c.discovered]
            cap = max(1, int(max_discovered))
            while len(discovered) >= cap:
                from app.paper.open_guard import has_open_exposure

                victim = next(
                    (c for c in discovered if not has_open_exposure(c.symbol, c.mint)),
                    None,
                )
                if victim is None:
                    discovered[0].discovered = False
                else:
                    self._drop_locked(victim.symbol, victim.mint)
                discovered = [c for c in self._curves.values() if c.discovered]
            ticker = "".join(ch for ch in (base or f"M{mint[-4:]}").upper() if ch.isalnum())[:16]
            if not ticker:
                ticker = f"M{mint[-4:]}".upper()
            n = 0
            candidate = ticker
            while f"{candidate}/SOL" in self._curves:
                n += 1
                candidate = f"{ticker[: max(1, 16 - len(str(n)))]}{n}"
            if reserves and reserves.get("virtual_sol") and reserves.get("virtual_token"):
                vs = int(reserves["virtual_sol"])
                vt = int(reserves["virtual_token"])
                rs = int(reserves.get("real_sol") or 0)
                rt = int(reserves.get("real_token") or INITIAL_REAL_TOKEN_RESERVES)
            else:
                vs, vt, rs, rt = (
                    INITIAL_VIRTUAL_SOL_RESERVES,
                    INITIAL_VIRTUAL_TOKEN_RESERVES,
                    0,
                    INITIAL_REAL_TOKEN_RESERVES,
                )
            if rt <= 0:
                return None
            symbol = f"{candidate}/SOL"
            now = int(time.time() * 1000)
            curve = _Curve(
                mint=mint,
                symbol=symbol,
                base=candidate,
                virtual_sol=vs,
                virtual_token=vt,
                real_sol=rs,
                real_token=rt,
                complete=False,
                discovered=source not in {"", "watch"},
                source=source,
                creator=creator,
                updated_ts=now,
            )
            self._curves[symbol] = curve
            self._by_mint[mint] = symbol
            self._trades[symbol] = []
            self._candles[symbol] = {iv: [] for iv in INTERVAL_MS}
            return curve

    def _drop_locked(self, symbol: str, mint: str) -> None:
        self._curves.pop(symbol, None)
        self._by_mint.pop(mint, None)
        self._trades.pop(symbol, None)
        self._candles.pop(symbol, None)

    def get_pumpfun_snapshot(self, symbol: str) -> PumpfunPaperSnapshot | None:
        with self._lock:
            c = self._curves.get(symbol)
            if c is None:
                return None
            px = price_sol(c.virtual_sol, c.virtual_token)
            return PumpfunPaperSnapshot(
                mint=c.mint,
                symbol=c.symbol,
                phase="graduating" if c.complete else "curve",
                progress_bps=progress_bps(c.real_token),
                complete=c.complete,
                migrated=False,
                virtual_sol_reserves=str(c.virtual_sol),
                virtual_token_reserves=str(c.virtual_token),
                real_sol_reserves=str(c.real_sol),
                real_token_reserves=str(c.real_token),
                token_total_supply=str(c.token_total_supply),
                price_sol=px,
                price_sol_str=price_sol_str(c.virtual_sol, max(c.virtual_token, 1)),
                market_cap_sol=market_cap_sol(c.virtual_sol, max(c.virtual_token, 1), c.token_total_supply),
                updated_ts=c.updated_ts,
                synthetic=False,
            )

    def get_recent_trades(self, symbol: str) -> list[dict]:
        with self._lock:
            return list(self._trades.get(symbol, []))

    def snapshot_book(self, symbol: str) -> dict:
        snap = self.get_pumpfun_snapshot(symbol)
        mid = float(snap.price_sol) if snap else 0.0
        return {
            "symbol": symbol,
            "bids": [],
            "asks": [],
            "mid": mid,
            "spread_bps": 0.0,
            "synthetic": False,
        }

    def first_trade_after(self, symbol: str, ts_ms: int) -> Optional[dict]:
        """Earliest observed print at or after ``ts_ms``. None if the tape is quiet."""
        with self._lock:
            rows = [r for r in self._trades.get(symbol, []) if int(r.get("ts") or 0) >= int(ts_ms)]
        if not rows:
            return None
        rows.sort(key=lambda r: int(r.get("ts") or 0))
        return dict(rows[0])

    def apply_observed_trade(self, row: dict[str, Any]) -> Optional[dict]:
        """Apply one real print. Missing reserves leave the price where it is."""
        mint = str(row.get("mint") or "").strip()
        side = str(row.get("side") or "").strip().lower()
        if side not in {"buy", "sell"} or not mint:
            return None
        vs = row.get("virtual_sol_reserves")
        vt = row.get("virtual_token_reserves")
        if vs is None or vt is None:
            return None
        vs_i = int(vs)
        vt_i = int(vt)
        if vs_i <= 0 or vt_i <= 0:
            return None
        with self._lock:
            symbol = self._by_mint.get(mint)
            if symbol is None:
                return None
            c = self._curves[symbol]
            c.virtual_sol = vs_i
            c.virtual_token = vt_i
            if row.get("real_sol_reserves") is not None:
                c.real_sol = int(row["real_sol_reserves"])
            if row.get("real_token_reserves") is not None:
                c.real_token = int(row["real_token_reserves"])
                c.complete = c.real_token <= 0
            ts = int(row.get("ts") or int(time.time() * 1000))
            c.updated_ts = ts
            px = price_sol(c.virtual_sol, c.virtual_token)
            sol_amount = float(row.get("sol_amount") or 0.0)
            token_amount = int(row.get("token_amount") or 0)
            qty = (token_amount / LAMPORTS_PER_SOL) if token_amount else float(row.get("qty") or 0.0)
            tick = PumpfunTradeTick(
                mint=c.mint,
                symbol=c.symbol,
                ts=ts,
                side=side,  # type: ignore[arg-type]
                price=px,
                qty=qty,
                sol_amount=sol_amount,
                signature=str(row.get("signature") or "") or None,
                phase="curve",
            )
            dumped = tick.model_dump()
            dumped["virtual_sol_reserves"] = str(c.virtual_sol)
            dumped["virtual_token_reserves"] = str(c.virtual_token)
            dumped["real_sol_reserves"] = str(c.real_sol)
            dumped["real_token_reserves"] = str(c.real_token)
            dumped["synthetic"] = False
            buf = self._trades.setdefault(symbol, [])
            buf.insert(0, dumped)
            del buf[_TRADE_CAP:]
            self._push_candle_locked(symbol, ts, px, abs(qty))
            return dumped

    def observe_portal_message(self, msg: dict[str, Any]) -> Optional[dict]:
        if not isinstance(msg, dict):
            return None
        pool = str(msg.get("pool") or "").strip().lower()
        if pool not in _PUMP_POOLS:
            return None
        side = str(msg.get("txType") or msg.get("side") or "").strip().lower()
        mint = str(msg.get("mint") or "").strip()
        vs = _as_int_reserve(msg.get("vSolInBondingCurve"), sol_unit=True)
        vt = _as_int_reserve(msg.get("vTokensInBondingCurve"))
        if vs <= 0 or vt <= 0:
            vs = _as_int_reserve(msg.get("virtual_sol_reserves"), sol_unit=True)
            vt = _as_int_reserve(msg.get("virtual_token_reserves"))
        rs = _as_int_reserve(msg.get("real_sol_reserves") or msg.get("solInBondingCurve"), sol_unit=True)
        rt = _as_int_reserve(msg.get("real_token_reserves"))
        ts_raw = msg.get("timestamp") or msg.get("ts")
        try:
            ts = int(ts_raw) if ts_raw is not None else int(time.time() * 1000)
        except (TypeError, ValueError):
            ts = int(time.time() * 1000)
        if ts < 10_000_000_000:
            ts *= 1000
        sol_amount = msg.get("solAmount")
        try:
            sol_f = float(sol_amount) if sol_amount is not None else 0.0
        except (TypeError, ValueError):
            sol_f = 0.0
        token_raw = msg.get("tokenAmount")
        try:
            token_amount = int(float(token_raw)) if token_raw is not None else 0
        except (TypeError, ValueError):
            token_amount = 0
        if token_amount and token_amount < 10_000_000_000:
            token_amount *= 1_000_000
        return self.apply_observed_trade(
            {
                "mint": mint,
                "side": side,
                "ts": ts,
                "virtual_sol_reserves": vs,
                "virtual_token_reserves": vt,
                "real_sol_reserves": rs,
                "real_token_reserves": rt if rt else None,
                "sol_amount": sol_f,
                "token_amount": token_amount,
                "signature": msg.get("signature"),
            }
        )

    def observe_trade_event(self, event: dict[str, Any], *, ts_ms: Optional[int] = None) -> Optional[dict]:
        ts = int(ts_ms if ts_ms is not None else int(event.get("timestamp") or 0) * 1000)
        if ts < 10_000_000_000:
            ts *= 1000
        sol = int(event.get("sol_amount") or 0)
        return self.apply_observed_trade(
            {
                "mint": event.get("mint"),
                "side": "buy" if event.get("is_buy") else "sell",
                "ts": ts or int(time.time() * 1000),
                "virtual_sol_reserves": int(event.get("virtual_sol_reserves") or 0),
                "virtual_token_reserves": int(event.get("virtual_token_reserves") or 0),
                "real_sol_reserves": int(event.get("real_sol_reserves") or 0),
                "real_token_reserves": int(event.get("real_token_reserves") or 0),
                "sol_amount": sol / LAMPORTS_PER_SOL,
                "token_amount": int(event.get("token_amount") or 0),
            }
        )

    def _push_candle_locked(self, symbol: str, ts: int, px: float, vol: float) -> None:
        book = self._candles.setdefault(symbol, {iv: [] for iv in INTERVAL_MS})
        for iv, ms in INTERVAL_MS.items():
            bucket = (ts // ms) * ms
            bars = book.setdefault(iv, [])
            if bars and bars[-1].t == bucket:
                last = bars[-1]
                bars[-1] = Candle(
                    symbol=symbol,
                    interval=iv,
                    t=bucket,
                    o=last.o,
                    h=max(last.h, px),
                    l=min(last.l, px),
                    c=px,
                    v=last.v + vol,
                )
            else:
                bars.append(Candle(symbol=symbol, interval=iv, t=bucket, o=px, h=px, l=px, c=px, v=vol))
                if len(bars) > 180:
                    del bars[:-180]

    async def stream(self, channel: str, symbol: str, interval: str | None = None) -> AsyncIterator[dict]:
        snap = self.get_pumpfun_snapshot(symbol)
        if snap is not None:
            yield {"type": "pumpfun_curve", "payload": snap.model_dump()}
        seen: set[int] = set()
        while True:
            await asyncio.sleep(0.5)
            if channel == "trades":
                for row in reversed(self.get_recent_trades(symbol)):
                    ts = int(row.get("ts") or 0)
                    if ts in seen:
                        continue
                    seen.add(ts)
                    yield {
                        "type": "trade",
                        "payload": {
                            "symbol": row.get("symbol"),
                            "ts": ts,
                            "price": row.get("price"),
                            "qty": row.get("qty"),
                            "side": row.get("side"),
                            "phase": row.get("phase"),
                            "synthetic": False,
                        },
                    }
            else:
                fresh = self.get_pumpfun_snapshot(symbol)
                if fresh is not None:
                    yield {"type": "pumpfun_curve", "payload": fresh.model_dump()}

    def stop_feed(self) -> None:
        self._feed_running = False

    async def run_feed(self) -> None:
        """Background read of real trades. Never submits a transaction."""
        self._feed_running = True
        if not feed_enabled():
            return
        while self._feed_running:
            try:
                await self._portal_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("live paper portal feed failed; trying logsSubscribe")
                try:
                    await self._logs_once()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("live paper logs feed failed")
                    await asyncio.sleep(5.0)

    def _watched_mints(self) -> list[str]:
        with self._lock:
            return [c.mint for c in self._curves.values()]

    async def _portal_once(self) -> None:
        import websockets

        from app.discovery import build_portal_ws_uri, portal_api_key

        mints = self._watched_mints()
        if not mints:
            await asyncio.sleep(1.0)
            return
        uri = build_portal_ws_uri(key=portal_api_key())
        async with websockets.connect(uri, ping_interval=20, ping_timeout=20) as ws:
            await ws.send(json.dumps({"method": "subscribeTokenTrade", "keys": mints}))
            while self._feed_running:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=60)
                except asyncio.TimeoutError:
                    continue
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if isinstance(msg, dict):
                    self.observe_portal_message(msg)

    async def _logs_once(self) -> None:
        import websockets

        http = (os.getenv("SOLANA_RPC_URL") or "https://api.mainnet-beta.solana.com").strip()
        parsed = urlparse(http)
        scheme = "wss" if parsed.scheme in {"https", "wss"} else "ws"
        uri = urlunparse((scheme, parsed.netloc, parsed.path or "/", "", parsed.query, ""))
        log.info("live paper logsSubscribe mentions=%s", PUMP_PROGRAM_ID)
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
            while self._feed_running:
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
                event = extract_trade_from_logs([str(x) for x in logs])
                if not event:
                    continue
                watched = set(self._watched_mints())
                if event.get("mint") not in watched:
                    continue
                self.observe_trade_event(event)
