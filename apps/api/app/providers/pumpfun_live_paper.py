"""Real-market paper provider (``DATA_PROVIDER=pumpfun_live_paper``).

Prices, prints, and reserves come only from observed on-chain trades:

* PumpPortal ``subscribeTokenTrade`` over the public data websocket (the
  ``PUMPFUN_PORTAL_API_KEY`` is used when configured; a rejected key falls
  back to the keyless public data API), and/or
* Solana ``logsSubscribe`` on the pump program, decoding ``TradeEvent``.
  When discovery already runs ``logs`` it forwards TradeEvents here, so no
  second RPC websocket is opened.

There is no RNG, no interpolation and no synthetic print: if nobody trades a
curve, its price does not move. Mints are only tradable after a read-only
``getAccountInfo`` confirms the pump bonding curve exists, is owned by the
pump program, and is not complete (``app.providers.pump_verify``). Mints seen
in a decoded pump ``Create`` event are pump-created by construction.

This provider never signs or sends a transaction; ``liveEnabled`` is untouched.
"""
from __future__ import annotations

import asyncio
import itertools
import json
import logging
import os
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, AsyncIterator, Callable, Optional
from urllib.parse import urlparse, urlunparse

from app.models.contracts import (
    Candle,
    Fill,
    PumpfunPaperSnapshot,
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
    TOKEN_DECIMALS,
    TOKEN_TOTAL_SUPPLY,
    market_cap_sol,
    price_sol,
    price_sol_str,
    progress_bps,
)
from app.providers.pumpfun_decode import PUMP_PROGRAM_ID, extract_trades_from_logs

log = logging.getLogger("auu.pumpfun_live_paper")

MARKET_SOURCE = "real"
INTERVAL_MS = {
    "1s": 1_000,
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "1h": 3_600_000,
}
# Pump bonding-curve pools as PumpPortal labels them. Anything else (pump-amm,
# raydium, bonk, ...) is not a live bonding-curve print.
PUMP_CURVE_POOLS = {"pump", "pumpfun", "pump.fun", ""}
TRADE_CAP = 600
_SIG_SEEN_CAP = 50_000
# Virtual reserves minus real reserves are constant for the standard curve.
_VT_OFFSET = INITIAL_VIRTUAL_TOKEN_RESERVES - INITIAL_REAL_TOKEN_RESERVES
_VS_OFFSET = INITIAL_VIRTUAL_SOL_RESERVES
# PumpPortal reports token reserves in UI units (6 decimals). Raw curve
# reserves are ~2.8e14..1.07e15, UI values ~2.8e8..1.07e9.
_UI_TOKEN_MAX = 1e12
FEED_MODES = ("auto", "portal", "logs", "off")


def feed_mode() -> str:
    legacy = (os.getenv("AUU_LIVE_PAPER_FEED") or "").strip().lower()
    if legacy in {"0", "off", "false", "no"}:
        return "off"
    raw = (os.getenv("LIVE_PAPER_FEED") or "auto").strip().lower()
    return raw if raw in FEED_MODES else "auto"


def feed_enabled() -> bool:
    return feed_mode() != "off"


def sol_to_lamports(val: Any) -> int:
    """SOL (float, PumpPortal) or lamports (int >= 1e6) → lamports."""
    if val is None or val == "":
        return 0
    try:
        n = float(val)
    except (TypeError, ValueError):
        return 0
    if n <= 0:
        return 0
    if n < 1_000_000:
        return int(round(n * LAMPORTS_PER_SOL))
    return int(n)


def tokens_to_raw(val: Any) -> int:
    """UI token amount (PumpPortal) or raw base units → raw base units."""
    if val is None or val == "":
        return 0
    try:
        n = float(val)
    except (TypeError, ValueError):
        return 0
    if n <= 0:
        return 0
    if n < _UI_TOKEN_MAX:
        return int(round(n * (10**TOKEN_DECIMALS)))
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
    registered_ts: int = 0
    last_trade_ts: int = 0
    verified: bool = False
    # True once the reserves are observed (a real print, the bonding-curve
    # account, or reserves carried by the discovery event). A logs ``Create``
    # carries none, and the 30-SOL template is not every curve's start, so
    # such a curve has no mark until its first real trade.
    priced: bool = False
    verify_reason: str = "pending"
    verify_attempts: int = 0
    next_verify_ts: int = 0
    protocol_fee_bps: Optional[int] = None
    creator_fee_bps: Optional[int] = None
    token_total_supply: int = TOKEN_TOTAL_SUPPLY


def _default_verifier() -> Callable[[str], Any]:
    from app.providers.pump_verify import http_rpc_call, verify_pump_mint

    rpc = http_rpc_call()
    return lambda mint: verify_pump_mint(mint, rpc)


class PumpfunLivePaperProvider(MarketDataProvider):
    """Watch-list of real bonding curves. The price moves only on observed prints."""

    name = "pumpfun_live_paper"

    def __init__(
        self,
        watch_mints: str | None = None,
        *,
        verifier: Optional[Callable[[str], Any]] = None,
        clock: Optional[Callable[[], int]] = None,
    ):
        self._lock = threading.RLock()
        self._curves: dict[str, _Curve] = {}
        self._by_mint: dict[str, str] = {}
        self._trades: dict[str, list[dict]] = {}
        self._candles: dict[str, dict[str, list[Candle]]] = {}
        self._rejected: dict[str, str] = {}
        self._seq = itertools.count(1)
        self._sig_seen: "OrderedDict[tuple[str, str, str], dict[str, int]]" = OrderedDict()
        self.duplicates_dropped = 0
        self.trades_by_feed: dict[str, int] = {}
        # Cumulative catalog counters (evictions do not decrease them).
        self.registered_by_source: dict[str, int] = {}
        self.verified_by_rpc = 0
        self.rejected_by_rpc = 0
        self.evicted = 0
        self._verifier = verifier
        self._clock = clock or (lambda: int(time.time() * 1000))
        self._feed_running = False
        self.feed_status = "idle"
        self.trades_seen = 0
        self.portal_sync_sec = 2.0
        raw = watch_mints if watch_mints is not None else os.getenv("PUMPFUN_WATCH_MINTS", "")
        for part in (raw or "").split(","):
            item = part.strip()
            if not item:
                continue
            bits = [b.strip() for b in item.split(":") if b.strip()]
            mint = bits[0]
            base = bits[1].replace("/SOL", "") if len(bits) > 1 else ""
            self.register_watch_mint(mint, base=base or None, source="watch")

    # ------------------------------------------------------------------ catalog
    def _tradable(self, c: _Curve) -> bool:
        return c.verified and c.priced

    def list_symbols(self) -> list[SymbolInfo]:
        with self._lock:
            return [
                SymbolInfo(symbol=c.symbol, base=c.base, quote="SOL", kind="pumpfun_curve", mint=c.mint)
                for c in self._curves.values()
                if self._tradable(c)
            ]

    def pending_mints(self) -> list[str]:
        with self._lock:
            return [c.mint for c in self._curves.values() if not c.verified]

    def rejected_mints(self) -> dict[str, str]:
        with self._lock:
            return dict(self._rejected)

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
            return {
                "discovered": bool(c.discovered),
                "source": c.source,
                "creator": c.creator,
                "verified": bool(c.verified),
                "verify_reason": c.verify_reason,
                "market_source": MARKET_SOURCE,
            }

    # ------------------------------------------------------------ registration
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
        """Add a mint to the watch list.

        Demo mints, complete curves and mints that already failed the pump
        bonding-curve check are refused. ``source="logs"`` means the mint came
        from a decoded pump program ``Create`` event and is tradable at once;
        every other source waits for :meth:`verify_pending`.
        """
        mint = (mint or "").strip()
        if not mint or _demo_mint(mint) or complete or int(progress_bps) >= 10_000:
            return None
        with self._lock:
            if mint in self._rejected:
                return None
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
                    # Every discovered curve still backs a paper/live/shadow
                    # position: keep them all and only stop counting the oldest.
                    discovered[0].discovered = False
                else:
                    self._drop_locked(victim.symbol, victim.mint)
                    self.evicted += 1
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
            now = self._clock()
            from_create = source == "logs"
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
                registered_ts=now,
                verified=from_create,
                priced=bool(reserves and reserves.get("virtual_sol") and reserves.get("virtual_token")),
                verify_reason="pump_create_event" if from_create else "pending",
            )
            self._curves[symbol] = curve
            self._by_mint[mint] = symbol
            self._trades[symbol] = []
            self._candles[symbol] = {iv: [] for iv in INTERVAL_MS}
            key = source or "watch"
            self.registered_by_source[key] = self.registered_by_source.get(key, 0) + 1
            return curve

    def _drop_locked(self, symbol: str, mint: str) -> None:
        self._curves.pop(symbol, None)
        self._by_mint.pop(mint, None)
        self._trades.pop(symbol, None)
        self._candles.pop(symbol, None)

    def verify_pending(self, *, limit: int = 8) -> dict[str, str]:
        """Check pending mints against the chain. Returns ``{mint: outcome}``.

        ``ok`` → tradable (reserves seeded from the account unless a real print
        already arrived); a hard failure drops the mint and remembers it;
        ``retry`` (RPC unreachable) keeps it pending and untradable.
        """
        now = self._clock()
        with self._lock:
            todo = [
                c.mint
                for c in self._curves.values()
                if not c.verified and c.next_verify_ts <= now
            ][: max(1, int(limit))]
        if not todo:
            return {}
        if self._verifier is None:
            self._verifier = _default_verifier()
        out: dict[str, str] = {}
        for mint in todo:
            try:
                res = self._verifier(mint)
            except Exception:
                res = {"ok": False, "retry": True, "reason": "verifier_error"}
            ok = bool(res.get("ok")) if isinstance(res, dict) else False
            retry = bool(res.get("retry")) if isinstance(res, dict) else True
            reason = str(res.get("reason") or "") if isinstance(res, dict) else "verifier_error"
            with self._lock:
                symbol = self._by_mint.get(mint)
                c = self._curves.get(symbol) if symbol else None
                if c is None:
                    continue
                if ok:
                    c.verified = True
                    c.verify_reason = reason or "pump_curve"
                    rsv = res.get("reserves") or {}
                    if c.last_trade_ts == 0 and rsv.get("virtual_sol") and rsv.get("virtual_token"):
                        c.priced = True
                        c.virtual_sol = int(rsv["virtual_sol"])
                        c.virtual_token = int(rsv["virtual_token"])
                        c.real_sol = int(rsv.get("real_sol") or 0)
                        c.real_token = int(rsv.get("real_token") or 0)
                        if rsv.get("token_total_supply"):
                            c.token_total_supply = int(rsv["token_total_supply"])
                        c.updated_ts = self._clock()
                    out[mint] = "ok"
                    self.verified_by_rpc += 1
                elif retry:
                    c.verify_attempts += 1
                    c.verify_reason = reason or "rpc_unavailable"
                    backoff = min(60_000, 2_000 * (2 ** min(c.verify_attempts, 5)))
                    c.next_verify_ts = self._clock() + backoff
                    out[mint] = "retry"
                else:
                    self._rejected[mint] = reason or "rejected"
                    if len(self._rejected) > 4_000:
                        for key in list(self._rejected)[:2_000]:
                            self._rejected.pop(key, None)
                    self._drop_locked(c.symbol, c.mint)
                    out[mint] = f"rejected:{reason}"
                    self.rejected_by_rpc += 1
        return out

    # ------------------------------------------------------------------ marks
    def get_pumpfun_snapshot(self, symbol: str) -> PumpfunPaperSnapshot | None:
        with self._lock:
            c = self._curves.get(symbol)
            if c is None or not self._tradable(c):
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
                creator_fee_bps=int(c.creator_fee_bps or 0),
                updated_ts=c.updated_ts,
                synthetic=False,
            )

    def fee_bps_for(self, symbol: str) -> tuple[Optional[int], Optional[int]]:
        """Last observed (protocol, creator) fee bps from a real TradeEvent."""
        with self._lock:
            c = self._curves.get(symbol)
            if c is None:
                return None, None
            return c.protocol_fee_bps, c.creator_fee_bps

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
        """Earliest observed print received at or after ``ts_ms``; None on a quiet tape."""
        with self._lock:
            rows = [r for r in self._trades.get(symbol, []) if int(r.get("ts") or 0) >= int(ts_ms)]
        if not rows:
            return None
        rows.sort(key=lambda r: (int(r.get("ts") or 0), int(r.get("seq") or 0)))
        return dict(rows[0])

    def apply_observed_trade(self, row: dict[str, Any]) -> Optional[dict]:
        """Apply one real print. Rows without reserves leave the price where it is.

        ``ts`` is the local receive time in ms (the same clock the strategy
        decides on). ``chain_ts`` keeps the on-chain block time when known.
        """
        mint = str(row.get("mint") or "").strip()
        side = str(row.get("side") or "").strip().lower()
        if side not in {"buy", "sell"} or not mint:
            return None
        try:
            vs_i = int(row.get("virtual_sol_reserves") or 0)
            vt_i = int(row.get("virtual_token_reserves") or 0)
        except (TypeError, ValueError):
            return None
        if vs_i <= 0 or vt_i <= 0:
            return None
        with self._lock:
            symbol = self._by_mint.get(mint)
            if symbol is None:
                return None
            sig = str(row.get("signature") or "") or None
            if self._duplicate_locked(sig, mint, side, str(row.get("feed") or "")):
                return None
            c = self._curves[symbol]
            c.priced = True
            c.virtual_sol = vs_i
            c.virtual_token = vt_i
            rs = row.get("real_sol_reserves")
            rt = row.get("real_token_reserves")
            c.real_sol = int(rs) if rs is not None else max(0, vs_i - _VS_OFFSET)
            c.real_token = int(rt) if rt is not None else max(0, vt_i - _VT_OFFSET)
            if c.real_token <= 0 or row.get("complete"):
                c.complete = True
            if row.get("protocol_fee_bps") is not None:
                c.protocol_fee_bps = int(row["protocol_fee_bps"])
            if row.get("creator_fee_bps") is not None:
                c.creator_fee_bps = int(row["creator_fee_bps"])
            ts = int(row.get("ts") or self._clock())
            c.updated_ts = ts
            c.last_trade_ts = ts
            px = price_sol(c.virtual_sol, c.virtual_token)
            sol_amount = float(row.get("sol_amount") or 0.0)
            token_amount = int(row.get("token_amount") or 0)
            qty = token_amount / LAMPORTS_PER_SOL if token_amount else 0.0
            dumped = {
                "mint": c.mint,
                "symbol": c.symbol,
                "ts": ts,
                "side": side,
                "price": px,
                "qty": qty,
                "sol_amount": sol_amount,
                "signature": sig,
                "phase": "graduating" if c.complete else "curve",
                "seq": next(self._seq),
                "chain_ts": row.get("chain_ts"),
                "virtual_sol_reserves": str(c.virtual_sol),
                "virtual_token_reserves": str(c.virtual_token),
                "real_sol_reserves": str(c.real_sol),
                "real_token_reserves": str(c.real_token),
                "protocol_fee_bps": c.protocol_fee_bps,
                "creator_fee_bps": c.creator_fee_bps,
                "feed": row.get("feed") or "",
                "synthetic": False,
                "market_source": MARKET_SOURCE,
            }
            buf = self._trades.setdefault(symbol, [])
            buf.insert(0, dumped)
            del buf[TRADE_CAP:]
            self._push_candle_locked(symbol, ts, px, abs(qty))
            self.trades_seen += 1
            feed = dumped["feed"] or "other"
            self.trades_by_feed[feed] = self.trades_by_feed.get(feed, 0) + 1
            return dumped

    def mark_graduated(self, mint: str) -> bool:
        with self._lock:
            symbol = self._by_mint.get((mint or "").strip())
            if not symbol:
                return False
            self._curves[symbol].complete = True
            return True

    def observe_portal_message(self, msg: dict[str, Any], *, recv_ts: Optional[int] = None) -> Optional[dict]:
        """PumpPortal ``subscribeTokenTrade`` message → one print.

        ``vSolInBondingCurve`` is SOL and ``vTokensInBondingCurve`` is UI
        tokens; both are converted to raw curve units. Real reserves are the
        virtual ones minus the standard curve offsets.
        """
        if not isinstance(msg, dict):
            return None
        mint = str(msg.get("mint") or "").strip()
        pool = str(msg.get("pool") or "").strip().lower()
        if pool not in PUMP_CURVE_POOLS:
            # A pump-amm / other-venue print means the curve is gone.
            if mint:
                self.mark_graduated(mint)
            return None
        side = str(msg.get("txType") or msg.get("side") or "").strip().lower()
        vs = sol_to_lamports(msg.get("vSolInBondingCurve"))
        vt = tokens_to_raw(msg.get("vTokensInBondingCurve"))
        sol_f = 0.0
        try:
            sol_f = float(msg.get("solAmount") or 0.0)
        except (TypeError, ValueError):
            sol_f = 0.0
        return self.apply_observed_trade(
            {
                "mint": mint,
                "side": side,
                "ts": recv_ts if recv_ts is not None else self._clock(),
                "virtual_sol_reserves": vs,
                "virtual_token_reserves": vt,
                "sol_amount": sol_f,
                "token_amount": tokens_to_raw(msg.get("tokenAmount")),
                "signature": msg.get("signature"),
                "feed": "pumpportal",
            }
        )

    def observe_trade_event(self, event: dict[str, Any], *, recv_ts: Optional[int] = None) -> Optional[dict]:
        """Decoded pump ``TradeEvent`` (raw units) → one print."""
        if not isinstance(event, dict):
            return None
        chain_ts = int(event.get("timestamp") or 0) * 1000 or None
        fee_bps = event.get("fee_basis_points")
        creator_bps = event.get("creator_fee_basis_points")
        return self.apply_observed_trade(
            {
                "mint": event.get("mint"),
                "side": "buy" if event.get("is_buy") else "sell",
                "ts": recv_ts if recv_ts is not None else self._clock(),
                "chain_ts": chain_ts,
                "virtual_sol_reserves": int(event.get("virtual_sol_reserves") or 0),
                "virtual_token_reserves": int(event.get("virtual_token_reserves") or 0),
                "real_sol_reserves": int(event.get("real_sol_reserves") or 0),
                "real_token_reserves": int(event.get("real_token_reserves") or 0),
                "sol_amount": int(event.get("sol_amount") or 0) / LAMPORTS_PER_SOL,
                "token_amount": int(event.get("token_amount") or 0),
                "protocol_fee_bps": int(fee_bps) if fee_bps is not None else None,
                "creator_fee_bps": int(creator_bps) if creator_bps is not None else None,
                "signature": event.get("signature"),
                "feed": "logs",
            }
        )

    def observe_logs(self, logs: list[str], *, signature: str | None = None, recv_ts: Optional[int] = None):
        """Apply every TradeEvent in one tx's logs; returns the last applied print."""
        last = None
        for event in extract_trades_from_logs([str(x) for x in logs or []]):
            if signature:
                event["signature"] = signature
            row = self.observe_trade_event(event, recv_ts=recv_ts)
            if row is not None:
                last = row
        return last

    def _duplicate_locked(self, sig: Optional[str], mint: str, side: str, feed: str) -> bool:
        """True when this print already arrived from another feed.

        The same on-chain trade can arrive via PumpPortal and via
        logsSubscribe. Per ``(signature, mint, side)`` count prints per feed;
        a print is new only while its feed's count exceeds every other
        feed's count (so a tx with two identical-side trades still yields two).
        """
        if not sig:
            return False
        key = (sig, mint, side)
        counts = self._sig_seen.get(key)
        if counts is None:
            counts = {}
            self._sig_seen[key] = counts
            if len(self._sig_seen) > _SIG_SEEN_CAP:
                self._sig_seen.popitem(last=False)
        n = counts.get(feed, 0) + 1
        counts[feed] = n
        other = max((v for f, v in counts.items() if f != feed), default=0)
        if n <= other:
            self.duplicates_dropped += 1
            return True
        return False

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
        last_seq = 0
        last_updated = snap.updated_ts if snap is not None else 0
        while True:
            await asyncio.sleep(0.5)
            if channel == "trades":
                for row in reversed(self.get_recent_trades(symbol)):
                    seq = int(row.get("seq") or 0)
                    if seq <= last_seq:
                        continue
                    last_seq = seq
                    yield {
                        "type": "trade",
                        "payload": {
                            "symbol": row.get("symbol"),
                            "ts": row.get("ts"),
                            "price": row.get("price"),
                            "qty": row.get("qty"),
                            "side": row.get("side"),
                            "phase": row.get("phase"),
                            "synthetic": False,
                            "market_source": MARKET_SOURCE,
                        },
                    }
            else:
                fresh = self.get_pumpfun_snapshot(symbol)
                if fresh is not None and fresh.updated_ts != last_updated:
                    last_updated = fresh.updated_ts
                    yield {"type": "pumpfun_curve", "payload": fresh.model_dump()}

    # ------------------------------------------------------------------- feed
    def feed_health(self) -> dict[str, Any]:
        with self._lock:
            pending = sum(1 for c in self._curves.values() if not c.verified)
            tradable = sum(1 for c in self._curves.values() if self._tradable(c))
            unpriced = sum(1 for c in self._curves.values() if c.verified and not c.priced)
        return {
            "feedMode": feed_mode(),
            "feedStatus": self.feed_status,
            "tradesSeen": self.trades_seen,
            "curvesTradable": tradable,
            "curvesPending": pending,
            "curvesAwaitingFirstPrint": unpriced,
            "mintsRejected": len(self._rejected),
            "tradesByFeed": dict(self.trades_by_feed),
            "duplicatesDropped": self.duplicates_dropped,
            "mintsRegisteredBySource": dict(self.registered_by_source),
            "mintsVerifiedByRpc": self.verified_by_rpc,
            "mintsRejectedByRpc": self.rejected_by_rpc,
            "mintsEvicted": self.evicted,
        }

    def stop_feed(self) -> None:
        self._feed_running = False

    async def run_feed(self) -> None:
        """Background read of real trades + curve verification. Never sends a transaction."""
        self._feed_running = True
        if not feed_enabled():
            self.feed_status = "off"
            return
        await asyncio.gather(self._verify_loop(), self._trade_loop())

    async def _verify_loop(self) -> None:
        while self._feed_running:
            try:
                if self.pending_mints():
                    await asyncio.to_thread(self.verify_pending)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("live paper curve verification failed")
            await asyncio.sleep(2.0)

    def _watched_mints(self) -> list[str]:
        with self._lock:
            return [c.mint for c in self._curves.values()]

    @staticmethod
    def _discovery_forwards_logs() -> bool:
        try:
            from app.discovery import get_discovery_if_running

            disc = get_discovery_if_running()
        except Exception:
            return False
        return bool(disc is not None and disc.mode == "logs")

    @staticmethod
    def _discovery_holds_portal() -> bool:
        try:
            from app.discovery import get_discovery_if_running

            disc = get_discovery_if_running()
        except Exception:
            return False
        return bool(disc is not None and disc.mode == "pumpportal" and disc.status_reason == "subscribed")

    async def _trade_loop(self) -> None:
        """Pick the trade source.

        ``auto`` prefers pump TradeEvents from ``logsSubscribe`` (free and
        complete: every pump trade), taken from discovery when its logs
        stream already runs, else from an own subscription. PumpPortal
        ``subscribeTokenTrade`` is metered per message on the key's linked
        wallet, so ``auto`` only falls back to it for 10 minutes after a logs
        failure; ``portal`` forces it.
        """
        mode = feed_mode()
        logs_block_until = 0.0
        while self._feed_running:
            try:
                if mode == "portal" or (mode == "auto" and time.time() < logs_block_until):
                    if mode == "auto" and self._discovery_forwards_logs():
                        logs_block_until = 0.0
                        continue
                    await self._portal_once()
                elif self._discovery_forwards_logs():
                    # Discovery's logsSubscribe already forwards TradeEvents here.
                    self.feed_status = "logs_via_discovery"
                    await asyncio.sleep(2.0)
                else:
                    await self._logs_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("live paper feed error (%s); switching source", type(exc).__name__)
                self.feed_status = "error"
                if mode == "auto" and time.time() >= logs_block_until:
                    logs_block_until = time.time() + 600.0
                elif mode == "auto":
                    logs_block_until = 0.0
                await asyncio.sleep(5.0)

    async def _portal_once(self) -> None:
        import websockets

        from app.discovery import (
            PORTAL_AUTH_REJECT_STATUSES,
            build_portal_ws_uri,
            portal_api_key,
            ws_http_status,
        )

        mints = self._watched_mints()
        if not mints:
            self.feed_status = "portal_waiting_for_mints"
            await asyncio.sleep(1.0)
            return
        base = (os.getenv("PUMPFUN_PORTAL_WS") or "").strip()
        key = portal_api_key()
        uris = [build_portal_ws_uri(key=key, base=base)] if key else []
        uris.append(build_portal_ws_uri(key="", base=base))  # public data API
        last_exc: Exception | None = None
        for uri in uris:
            try:
                await self._portal_session(websockets, uri)
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                last_exc = exc
                status = ws_http_status(exc)
                if status in PORTAL_AUTH_REJECT_STATUSES:
                    log.warning("live paper portal HTTP %s; trying keyless public data API (key not logged)", status)
                    continue
                raise
        if last_exc is not None:
            raise last_exc

    async def _portal_session(self, websockets: Any, uri: str) -> None:
        async with websockets.connect(uri, ping_interval=20, ping_timeout=20) as ws:
            subscribed = set(self._watched_mints())
            await ws.send(json.dumps({"method": "subscribeTokenTrade", "keys": sorted(subscribed)}))
            self.feed_status = "portal_subscribed"
            last_sync = time.monotonic()
            while self._feed_running:
                if feed_mode() == "auto" and self._discovery_forwards_logs():
                    return  # free logs stream is back; stop the metered one
                if time.monotonic() - last_sync >= self.portal_sync_sec:
                    last_sync = time.monotonic()
                    want = set(self._watched_mints())
                    add = sorted(want - subscribed)
                    drop = sorted(subscribed - want)
                    if add:
                        await ws.send(json.dumps({"method": "subscribeTokenTrade", "keys": add}))
                    if drop:
                        await ws.send(json.dumps({"method": "unsubscribeTokenTrade", "keys": drop}))
                    subscribed = want
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=max(0.05, self.portal_sync_sec))
                except asyncio.TimeoutError:
                    continue
                try:
                    msg = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    continue
                if isinstance(msg, dict) and msg.get("mint"):
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
            self.feed_status = "logs_subscribed"
            while self._feed_running:
                if self._discovery_forwards_logs():
                    return
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=60)
                except asyncio.TimeoutError:
                    continue
                try:
                    msg = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    continue
                if not isinstance(msg, dict):
                    continue
                value = ((msg.get("params") or {}).get("result") or {}).get("value") or {}
                logs = value.get("logs") or []
                if isinstance(logs, list) and not value.get("err"):
                    self.observe_logs(logs, signature=value.get("signature"))
