"""Local Pump.fun bonding-curve paper MarketDataProvider.

Seeded by PUMPFUN_WATCH_MINTS (or built-in demo mints). Simulates curve
reserves, progress_bps, and synthetic trades/candles. No wallet keys, no
RPC send, no sniper / bundle-tip path.
"""
from __future__ import annotations

import asyncio
import hashlib
import math
import os
import threading
import time
from dataclasses import dataclass
from typing import AsyncIterator

from app.models.contracts import (
    Candle,
    Fill,
    PumpfunPaperSnapshot,
    PumpfunTradeTick,
    RiskEvent,
    RiskOut,
    SignalEvent,
    SignalOut,
    SymbolInfo,
)
from app.providers.base import MarketDataProvider
from app.providers.pumpfun_curve_math import (
    AMM_INITIAL_TOKEN_RESERVES,
    DEFAULT_CREATOR_FEE_BPS,
    DEFAULT_PROTOCOL_FEE_BPS,
    INITIAL_REAL_TOKEN_RESERVES,
    INITIAL_VIRTUAL_SOL_RESERVES,
    INITIAL_VIRTUAL_TOKEN_RESERVES,
    TOKEN_DECIMALS,
    TOKEN_TOTAL_SUPPLY,
    apply_buy,
    apply_sell,
    market_cap_sol,
    price_sol,
    price_sol_str,
    progress_bps,
    reserves_at_progress_bps,
)
from app.providers.pumpfun_decode import PUMP_AMM_PROGRAM_ID

INTERVAL_MS = {
    "1s": 1_000,
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "1h": 3_600_000,
}

TRADE_EVERY_MS = 800
HISTORY_BARS = 180
TOKEN_SCALE = 10 ** TOKEN_DECIMALS  # raw → whole token


def _seed_int(s: str) -> int:
    return int(hashlib.sha256(s.encode()).hexdigest()[:8], 16)


class DetRNG:
    def __init__(self, seed: int):
        self.state = seed & 0xFFFFFFFF

    def next(self) -> float:
        self.state = (1664525 * self.state + 1013904223) & 0xFFFFFFFF
        return self.state / 0x100000000

    def uniform(self, a: float, b: float) -> float:
        return a + (b - a) * self.next()

    def gauss(self, mu: float = 0.0, sigma: float = 1.0) -> float:
        u1 = max(self.next(), 1e-12)
        u2 = self.next()
        z = math.sqrt(-2.0 * math.log(u1)) * math.cos(2 * math.pi * u2)
        return mu + sigma * z


@dataclass
class CurveMint:
    mint: str
    symbol: str
    base: str
    quote: str = "SOL"
    virtual_sol: int = INITIAL_VIRTUAL_SOL_RESERVES
    virtual_token: int = INITIAL_VIRTUAL_TOKEN_RESERVES
    real_sol: int = 0
    real_token: int = INITIAL_REAL_TOKEN_RESERVES
    token_total_supply: int = TOKEN_TOTAL_SUPPLY
    complete: bool = False
    migrated: bool = False
    amm_pool: str | None = None
    amm_sol: int = 0
    amm_token: int = 0
    creator_fee_bps: int = DEFAULT_CREATOR_FEE_BPS
    protocol_fee_bps: int = DEFAULT_PROTOCOL_FEE_BPS
    last_ms: int = 0
    last_price: float = 0.0
    discovered: bool = False
    discovery_source: str = ""
    creator: str = ""


@dataclass
class WatchSpec:
    mint: str
    base: str
    target_bps: int
    migrated: bool
    discovered: bool = False
    source: str = ""
    creator: str = ""
    reserves: dict[str, int] | None = None


# Built-in paper demos (not real mints). Empty PUMPFUN_WATCH_MINTS → these.
_BUILTIN: list[WatchSpec] = [
    WatchSpec("DemoMintPump11111111111111111111111111111", "PUMPDEMO", 4200, False),
    WatchSpec("DemoMintMoon11111111111111111111111111111", "MOONMOCK", 9700, False),
    WatchSpec("DemoMintGrad11111111111111111111111111111", "GRADMOCK", 10_000, True),
]


def _parse_watch_mints(raw: str | None) -> list[WatchSpec]:
    text = (raw or "").strip()
    if not text:
        return list(_BUILTIN)
    out: list[WatchSpec] = []
    for part in text.split(","):
        item = part.strip()
        if not item:
            continue
        bits = [b.strip() for b in item.split(":") if b.strip()]
        mint = bits[0]
        base = bits[1].replace("/SOL", "").upper() if len(bits) > 1 else f"M{mint[-4:]}".upper()
        if len(bits) > 2 and bits[2].isdigit():
            bps = max(0, min(10_000, int(bits[2])))
        else:
            bps = 500 + (_seed_int(mint) % 8500)
        migrated = bps >= 10_000 or (len(bits) > 3 and bits[3].lower() in {"migrated", "amm", "1", "true"})
        out.append(WatchSpec(mint=mint, base=base[:16], target_bps=bps, migrated=migrated))
    return out or list(_BUILTIN)


def _phase(c: CurveMint) -> str:
    if c.migrated:
        return "amm"
    if c.complete:
        return "graduating"
    return "curve"


def _demo_pool(mint: str) -> str:
    # Deterministic fake pool address for paper UI (not a real PDA).
    h = hashlib.sha256(f"pool:{mint}:{PUMP_AMM_PROGRAM_ID}".encode()).hexdigest()[:32]
    return f"pAMM{h}"


def _spot(c: CurveMint) -> float:
    if c.migrated and c.amm_token > 0:
        return price_sol(c.amm_sol, c.amm_token)
    return price_sol(c.virtual_sol, c.virtual_token)


def _raw_to_ui(raw: int) -> float:
    return raw / TOKEN_SCALE


class PumpfunPaperProvider(MarketDataProvider):
    name = "pumpfun_paper"

    def __init__(self, watch_mints: str | None = None):
        raw = watch_mints if watch_mints is not None else os.getenv("PUMPFUN_WATCH_MINTS", "")
        specs = _parse_watch_mints(raw)
        self._lock = threading.RLock()
        self._curves: dict[str, CurveMint] = {}
        self._by_mint: dict[str, str] = {}
        self._candles: dict[str, dict[str, list[Candle]]] = {}
        self._trades: dict[str, list[dict]] = {}
        self._signal_cache: dict[str, list[SignalEvent]] = {}
        self._fill_cache: dict[str, list[Fill]] = {}
        self._risk_cache: dict[str, list[RiskEvent]] = {}
        now = int(time.time() * 1000)
        for spec in specs:
            self._init_mint(spec, now)
        for sym in list(self._curves):
            self._build_overlays(sym)

    def _unique_base(self, base: str, mint: str) -> str:
        ticker = "".join(ch for ch in (base or "").replace("/SOL", "").upper() if ch.isalnum())[:16]
        if not ticker:
            ticker = f"M{(mint or 'XXXX')[-4:]}".upper()
        candidate = ticker
        n = 0
        while f"{candidate}/SOL" in self._curves:
            n += 1
            suf = str(n)
            candidate = f"{ticker[: max(1, 16 - len(suf))]}{suf}"
        return candidate

    def _init_mint(self, spec: WatchSpec, now_ms: int) -> None:
        base = self._unique_base(spec.base, spec.mint)
        symbol = f"{base}/SOL"
        migrated = bool(spec.migrated)
        target = 10_000 if migrated else spec.target_bps
        if spec.reserves and spec.reserves.get("virtual_sol") and spec.reserves.get("virtual_token"):
            vs = int(spec.reserves["virtual_sol"])
            vt = int(spec.reserves["virtual_token"])
            rs = int(spec.reserves.get("real_sol") or 0)
            rt = int(spec.reserves.get("real_token") or INITIAL_REAL_TOKEN_RESERVES)
        else:
            vs, vt, rs, rt = reserves_at_progress_bps(target)
        complete = rt <= 0 or target >= 10_000 or migrated
        if migrated:
            rt = 0
            complete = True
        c = CurveMint(
            mint=spec.mint,
            symbol=symbol,
            base=base,
            virtual_sol=vs,
            virtual_token=0 if migrated else vt,
            real_sol=0 if migrated else rs,
            real_token=0 if migrated else rt,
            complete=complete,
            migrated=migrated,
            amm_pool=_demo_pool(spec.mint) if migrated else None,
            amm_sol=rs if migrated else 0,
            amm_token=AMM_INITIAL_TOKEN_RESERVES if migrated else 0,
            last_ms=now_ms,
            discovered=bool(spec.discovered),
            discovery_source=spec.source or "",
            creator=spec.creator or "",
        )
        c.last_price = _spot(c)
        self._curves[symbol] = c
        self._by_mint[spec.mint] = symbol
        self._candles[symbol] = {iv: [] for iv in INTERVAL_MS}
        self._trades[symbol] = []
        self._seed_history(c, now_ms)
        # Prime tape at current spot without moving the seeded curve.
        px = max(_spot(c), 1e-18)
        phase = "amm" if c.migrated else "curve"
        primed: list[dict] = []
        for i in range(8):
            ts = now_ms - (8 - i) * TRADE_EVERY_MS
            side = "buy" if i % 2 == 0 else "sell"
            primed.append(
                PumpfunTradeTick(
                    mint=c.mint,
                    symbol=c.symbol,
                    ts=ts,
                    side=side,  # type: ignore[arg-type]
                    price=px,
                    qty=1_000.0 + i * 50,
                    sol_amount=px * (1_000.0 + i * 50),
                    phase=phase,  # type: ignore[arg-type]
                ).model_dump()
            )
        self._trades[symbol] = list(reversed(primed))
        c.last_ms = now_ms

    def watch_flags(self, symbol: str) -> dict:
        with self._lock:
            c = self._curves.get(symbol)
            if c is None:
                return {}
            return {
                "discovered": bool(c.discovered),
                "source": c.discovery_source,
                "creator": c.creator,
            }

    def _has_open_position(self, symbol: str, mint: str) -> bool:
        """True when paper or live still holds ``symbol`` or ``mint``.

        Discovery eviction used to ``_drop_locked`` the oldest discovered mint,
        which also deleted ``_trades``. A held mint then fell through to the
        orphan snapshot with ``buy_notional_1m == sell_notional_1m == 0``, so
        ``sell >= ratio * buy`` never started the sell-pressure timer.

        Sources: paper journal lots, live journal lots, and the process-wide
        strategy engine. A local engine in tests still records the paper lot.
        """
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
        return False

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
    ):
        """Add a discovered mint to the paper watchlist. Does not place orders."""
        mint = (mint or "").strip()
        if not mint:
            return None
        with self._lock:
            existing = self._by_mint.get(mint)
            if existing:
                return self._curves.get(existing)
            discovered = [c for c in self._curves.values() if c.discovered]
            cap = max(1, int(max_discovered))
            while len(discovered) >= cap:
                victim = next(
                    (c for c in discovered if not self._has_open_position(c.symbol, c.mint)),
                    None,
                )
                if victim is not None:
                    self._drop_locked(victim.symbol, victim.mint)
                else:
                    # Cap is full of open positions. Free the oldest discovery
                    # slot but keep the curve and trade buffer so tick() still
                    # sees a live tape (sell_pressure can fire).
                    discovered[0].discovered = False
                discovered = [c for c in self._curves.values() if c.discovered]
            ticker = (base or f"M{mint[-4:]}").replace("/SOL", "").upper()[:16]
            spec = WatchSpec(
                mint=mint,
                base=ticker,
                target_bps=max(0, min(10_000, int(progress_bps))),
                migrated=False,
                discovered=True,
                source=source,
                creator=creator,
                reserves=reserves,
            )
            now_ms = int(time.time() * 1000)
            self._init_mint(spec, now_ms)
            # Discovery is watchlist-only: seed candles/tape, skip demo-momentum overlays.
            sym = self._by_mint.get(mint)
            return self._curves.get(sym) if sym else None

    def _drop_locked(self, symbol: str, mint: str) -> None:
        self._curves.pop(symbol, None)
        self._by_mint.pop(mint, None)
        self._candles.pop(symbol, None)
        self._trades.pop(symbol, None)
        self._signal_cache.pop(symbol, None)
        self._fill_cache.pop(symbol, None)
        self._risk_cache.pop(symbol, None)

    def _seed_history(self, c: CurveMint, now_ms: int) -> None:
        iv = INTERVAL_MS["1m"]
        end_t = (now_ms // iv) * iv
        start_t = end_t - HISTORY_BARS * iv
        px = max(_spot(c), 1e-18)
        rng = DetRNG(_seed_int(f"hist:{c.mint}"))
        # Walk backward from current spot, then reverse.
        closes: list[tuple[int, float, float]] = []
        cur = px
        t = end_t
        while t >= start_t:
            vol = abs(rng.gauss(5e5, 2e5))
            closes.append((t, cur, vol))
            cur = max(cur * (1 + rng.gauss(0, 0.012)), px * 1e-6)
            t -= iv
        closes.reverse()
        # Rebase so last close == current spot.
        if closes:
            scale = px / closes[-1][1] if closes[-1][1] else 1.0
            closes = [(t, p * scale, v) for t, p, v in closes]
        bars: list[Candle] = []
        prev = closes[0][1] if closes else px
        for t, close, vol in closes:
            o = prev
            wiggle = abs((close - o) * 0.6) + close * 0.002
            h = max(o, close) + wiggle
            l = max(min(o, close) - wiggle, close * 1e-9)
            bars.append(
                Candle(symbol=c.symbol, interval="1m", t=t, o=o, h=h, l=l, c=close, v=vol)
            )
            prev = close
        self._candles[c.symbol]["1m"] = bars
        # Derive coarser intervals from 1m; seed 1s from last close.
        self._rebuild_higher_tfs(c.symbol)
        last = bars[-1] if bars else None
        if last:
            s_iv = INTERVAL_MS["1s"]
            st = (now_ms // s_iv) * s_iv
            self._candles[c.symbol]["1s"] = [
                Candle(
                    symbol=c.symbol,
                    interval="1s",
                    t=st,
                    o=last.c,
                    h=last.c,
                    l=last.c,
                    c=last.c,
                    v=0.0,
                )
            ]

    def _rebuild_higher_tfs(self, symbol: str) -> None:
        src = self._candles[symbol].get("1m") or []
        for label, ms in INTERVAL_MS.items():
            if label in {"1s", "1m"}:
                continue
            grouped: dict[int, list[Candle]] = {}
            for b in src:
                grouped.setdefault((b.t // ms) * ms, []).append(b)
            out: list[Candle] = []
            for t in sorted(grouped):
                chunk = grouped[t]
                out.append(
                    Candle(
                        symbol=symbol,
                        interval=label,
                        t=t,
                        o=chunk[0].o,
                        h=max(x.h for x in chunk),
                        l=min(x.l for x in chunk),
                        c=chunk[-1].c,
                        v=sum(x.v for x in chunk),
                    )
                )
            self._candles[symbol][label] = out

    def list_symbols(self) -> list[SymbolInfo]:
        return [
            SymbolInfo(
                symbol=c.symbol,
                base=c.base,
                quote=c.quote,
                kind="pumpfun_curve",
                mint=c.mint,
            )
            for c in self._curves.values()
        ]

    def get_pumpfun_snapshot(self, symbol: str) -> PumpfunPaperSnapshot | None:
        with self._lock:
            self._advance_locked(symbol, int(time.time() * 1000))
            c = self._curves.get(symbol)
            if c is None:
                return None
            return self._snapshot_locked(c)

    def snapshot_to_pump_ctx(self, symbol: str):
        snap = self.get_pumpfun_snapshot(symbol)
        return snap.to_pump_ctx() if snap else None

    def get_recent_trades(self, symbol: str) -> list[dict]:
        with self._lock:
            self._advance_locked(symbol, int(time.time() * 1000))
            return list(self._trades.get(symbol, []))

    def _snapshot_locked(self, c: CurveMint) -> PumpfunPaperSnapshot:
        px = _spot(c)
        vs, vt = c.virtual_sol, c.virtual_token
        mcap_vs, mcap_vt = (c.amm_sol, c.amm_token) if c.migrated else (vs, vt)
        return PumpfunPaperSnapshot(
            mint=c.mint,
            symbol=c.symbol,
            phase=_phase(c),  # type: ignore[arg-type]
            progress_bps=progress_bps(c.real_token) if not c.migrated else 10_000,
            complete=c.complete,
            migrated=c.migrated,
            virtual_sol_reserves=str(vs),
            virtual_token_reserves=str(vt),
            real_sol_reserves=str(c.real_sol),
            real_token_reserves=str(c.real_token),
            token_total_supply=str(c.token_total_supply),
            price_sol=px,
            price_sol_str=price_sol_str(
                mcap_vs if mcap_vt else vs, mcap_vt if mcap_vt else max(vt, 1)
            ),
            market_cap_sol=market_cap_sol(
                mcap_vs if mcap_vt else vs,
                mcap_vt if mcap_vt else max(vt, 1),
                c.token_total_supply,
            ),
            creator_fee_bps=c.creator_fee_bps,
            pool=c.amm_pool,
            updated_ts=c.last_ms,
            synthetic=True,
        )

    def get_candles(
        self, symbol: str, interval: str = "1m", from_ts: int | None = None, to_ts: int | None = None
    ) -> list[Candle]:
        with self._lock:
            self._advance_locked(symbol, int(time.time() * 1000))
            iv = interval if interval in INTERVAL_MS else "1m"
            out = list(self._candles.get(symbol, {}).get(iv, []))
        if from_ts is not None:
            out = [c for c in out if c.t >= from_ts]
        if to_ts is not None:
            out = [c for c in out if c.t <= to_ts]
        return out

    def snapshot_book(self, symbol: str) -> dict:
        with self._lock:
            self._advance_locked(symbol, int(time.time() * 1000))
            c = self._curves.get(symbol)
            if c is None:
                return {"symbol": symbol, "bids": [], "asks": [], "mid": 0.0, "spread_bps": 0.0, "synthetic": True}
            mid = max(_spot(c), 1e-18)
            prog = progress_bps(c.real_token) if not c.migrated else 10_000
            spread_bps = 20.0 + prog / 10_000 * 50.0
            if c.complete and not c.migrated:
                spread_bps += 25.0
            half = mid * (spread_bps / 1e4) / 2
            remaining_ui = _raw_to_ui(c.real_token) if not c.migrated else _raw_to_ui(c.amm_token)
            bids, asks = [], []
            for i in range(10):
                step = half * (1 + i * 0.35)
                sz = max(remaining_ui * (0.02 / (1 + i)), 1.0)
                bids.append({"price": max(mid - half - step, mid * 1e-9), "size": sz})
                asks.append({"price": mid + half + step, "size": sz})
            return {
                "symbol": symbol,
                "bids": bids,
                "asks": asks,
                "mid": mid,
                "spread_bps": spread_bps,
                "synthetic": True,
            }

    def _apply_trade_locked(self, c: CurveMint, ts: int) -> PumpfunTradeTick | None:
        rng = DetRNG(_seed_int(f"tr:{c.mint}:{ts}"))
        side = "buy" if rng.next() > 0.42 else "sell"
        if c.migrated:
            return self._amm_trade_locked(c, ts, side, rng)
        if c.complete:
            # graduating: no curve buys; rare sells already drained — idle ticks
            return None
        if side == "buy":
            sol = int(rng.uniform(0.02, 0.35) * 1_000_000_000)
            q = apply_buy(
                c.virtual_sol,
                c.virtual_token,
                c.real_sol,
                c.real_token,
                sol,
                c.protocol_fee_bps,
                c.creator_fee_bps,
            )
            if q.tokens_delta <= 0:
                return None
            c.virtual_sol = q.virtual_sol_reserves
            c.virtual_token = q.virtual_token_reserves
            c.real_sol = q.real_sol_reserves
            c.real_token = q.real_token_reserves
            c.complete = q.complete
            px = _spot(c)
            c.last_price = px
            self._push_candle_locked(c.symbol, ts, px, _raw_to_ui(q.tokens_delta))
            tick = PumpfunTradeTick(
                mint=c.mint,
                symbol=c.symbol,
                ts=ts,
                side="buy",
                price=px,
                qty=_raw_to_ui(q.tokens_delta),
                sol_amount=q.sol_delta / 1_000_000_000,
                phase="curve",
            )
        else:
            # sell a small slice of remaining sold tokens
            sold = INITIAL_REAL_TOKEN_RESERVES - c.real_token
            if sold <= TOKEN_SCALE:
                return None
            amt = int(min(sold * rng.uniform(0.0005, 0.004), sold * 0.01))
            amt = max(amt, TOKEN_SCALE)
            q = apply_sell(
                c.virtual_sol,
                c.virtual_token,
                c.real_sol,
                c.real_token,
                amt,
                c.protocol_fee_bps,
                c.creator_fee_bps,
            )
            if q.tokens_delta == 0:
                return None
            c.virtual_sol = q.virtual_sol_reserves
            c.virtual_token = q.virtual_token_reserves
            c.real_sol = q.real_sol_reserves
            c.real_token = q.real_token_reserves
            c.complete = q.complete
            px = _spot(c)
            c.last_price = px
            self._push_candle_locked(c.symbol, ts, px, _raw_to_ui(abs(q.tokens_delta)))
            tick = PumpfunTradeTick(
                mint=c.mint,
                symbol=c.symbol,
                ts=ts,
                side="sell",
                price=px,
                qty=_raw_to_ui(abs(q.tokens_delta)),
                sol_amount=abs(q.sol_delta) / 1_000_000_000,
                phase="curve",
            )
        dumped = tick.model_dump()
        buf = self._trades.setdefault(c.symbol, [])
        buf.insert(0, dumped)
        del buf[80:]
        return tick

    def _amm_trade_locked(self, c: CurveMint, ts: int, side: str, rng: DetRNG) -> PumpfunTradeTick | None:
        if c.amm_token <= 0 or c.amm_sol <= 0:
            return None
        sol = int(rng.uniform(0.03, 0.4) * 1_000_000_000)
        if side == "buy":
            tokens = sol * c.amm_token // (c.amm_sol + sol)
            if tokens <= 0:
                return None
            c.amm_sol += sol
            c.amm_token -= tokens
            qty = _raw_to_ui(tokens)
            sol_amt = sol / 1_000_000_000
        else:
            tokens = int(c.amm_token * rng.uniform(0.0004, 0.003))
            tokens = max(tokens, TOKEN_SCALE)
            gross = tokens * c.amm_sol // (c.amm_token + tokens)
            if gross <= 0 or gross >= c.amm_sol:
                return None
            c.amm_sol -= gross
            c.amm_token += tokens
            qty = _raw_to_ui(tokens)
            sol_amt = gross / 1_000_000_000
        px = _spot(c)
        c.last_price = px
        self._push_candle_locked(c.symbol, ts, px, qty)
        tick = PumpfunTradeTick(
            mint=c.mint,
            symbol=c.symbol,
            ts=ts,
            side=side,  # type: ignore[arg-type]
            price=px,
            qty=qty,
            sol_amount=sol_amt,
            phase="amm",
        )
        dumped = tick.model_dump()
        buf = self._trades.setdefault(c.symbol, [])
        buf.insert(0, dumped)
        del buf[80:]
        return tick

    def _push_candle_locked(self, symbol: str, ts: int, price: float, qty: float) -> None:
        for label, ms in INTERVAL_MS.items():
            bucket = (ts // ms) * ms
            buf = self._candles.setdefault(symbol, {}).setdefault(label, [])
            if buf and buf[-1].t == bucket:
                last = buf[-1]
                last.h = max(last.h, price)
                last.l = min(last.l, price)
                last.c = price
                last.v += qty
            else:
                o = buf[-1].c if buf else price
                buf.append(
                    Candle(
                        symbol=symbol,
                        interval=label,
                        t=bucket,
                        o=o,
                        h=max(o, price),
                        l=min(o, price),
                        c=price,
                        v=qty,
                    )
                )
                if len(buf) > 360:
                    del buf[: len(buf) - 300]

    def _advance_locked(self, symbol: str, now_ms: int) -> list[PumpfunTradeTick]:
        c = self._curves.get(symbol)
        if c is None:
            return []
        if c.last_ms <= 0:
            c.last_ms = now_ms
            return []
        emitted: list[PumpfunTradeTick] = []
        t = c.last_ms + TRADE_EVERY_MS
        # Bound catch-up so a long pause doesn't spin.
        max_steps = 40
        steps = 0
        while t <= now_ms and steps < max_steps:
            tick = self._apply_trade_locked(c, t)
            if tick is not None:
                emitted.append(tick)
            c.last_ms = t
            t += TRADE_EVERY_MS
            steps += 1
        if now_ms > c.last_ms:
            c.last_ms = now_ms
        return emitted

    def _build_overlays(self, symbol: str) -> None:
        candles = self.get_candles(symbol, "1m")
        if len(candles) < 20:
            self._signal_cache[symbol] = []
            self._fill_cache[symbol] = []
            self._risk_cache[symbol] = []
            return
        sigs: list[SignalEvent] = []
        fills: list[Fill] = []
        risks: list[RiskEvent] = []
        n = 18
        sides = ["long", "short"]
        for i, bar in enumerate(candles):
            if i < 10 or i % n != 0:
                continue
            side = sides[(i // n) % 2]
            sig = SignalOut(
                side=side,  # type: ignore[arg-type]
                strength=min(0.45 + ((i // n) % 5) * 0.1, 0.95),
                reason=f"demo_{side}_curve_bar{i}",
                tags=["demo", "pumpfun_paper"],
            )
            sigs.append(SignalEvent(strategyId="demo-momentum-v0", symbol=symbol, t=bar.t, signal=sig))
            deny = ((i // n) % 4) == 3
            near = False
            curve = self._curves.get(symbol)
            if curve is not None:
                near = curve.migrated or curve.complete or progress_bps(curve.real_token) >= 9500
            if deny:
                risks.append(
                    RiskEvent(
                        strategyId="demo-momentum-v0",
                        symbol=symbol,
                        t=bar.t,
                        risk=RiskOut(
                            allow=False,
                            tags=["CURVE_NEAR_GRADUATION"] if near else ["COOLDOWN"],
                            notes="demo deny",
                        ),
                    )
                )
                continue
            risks.append(
                RiskEvent(
                    strategyId="demo-momentum-v0",
                    symbol=symbol,
                    t=bar.t,
                    risk=RiskOut(allow=True, tags=[], notes="ok"),
                )
            )
            delay = 1 + (i // n) % 2
            if i + delay < len(candles):
                fc = candles[i + delay]
                qty = 100.0 if side == "long" else -80.0
                fills.append(
                    Fill(
                        ts=fc.t,
                        price=fc.c,
                        qty=qty,
                        fee=abs(fc.c * qty) * 0.0015,
                        slippage_bps=8.0 + (i % 7),
                        tag="demo-momentum-v0",
                    )
                )
        self._signal_cache[symbol] = sigs
        self._fill_cache[symbol] = fills
        self._risk_cache[symbol] = risks

    def get_signals(
        self, symbol: str, from_ts: int | None = None, to_ts: int | None = None
    ) -> list[SignalEvent]:
        if symbol not in self._signal_cache:
            self._build_overlays(symbol)
        out = self._signal_cache.get(symbol, [])
        if from_ts is not None:
            out = [x for x in out if x.t >= from_ts]
        if to_ts is not None:
            out = [x for x in out if x.t <= to_ts]
        return out

    def get_fills(
        self, symbol: str, from_ts: int | None = None, to_ts: int | None = None
    ) -> list[Fill]:
        if symbol not in self._fill_cache:
            self._build_overlays(symbol)
        out = self._fill_cache.get(symbol, [])
        if from_ts is not None:
            out = [x for x in out if x.ts >= from_ts]
        if to_ts is not None:
            out = [x for x in out if x.ts <= to_ts]
        return out

    def get_risk_events(self, symbol: str) -> list[RiskEvent]:
        if symbol not in self._risk_cache:
            self._build_overlays(symbol)
        return self._risk_cache.get(symbol, [])

    async def stream(self, channel: str, symbol: str, interval: str | None = None) -> AsyncIterator[dict]:
        iv = interval or "1m"
        tick_n = 0
        snap = self.get_pumpfun_snapshot(symbol)
        if snap is not None:
            yield {"type": "pumpfun_curve", "payload": snap.model_dump()}

        if channel == "risk":
            risks = self.get_risk_events(symbol)
            pick = next((r for r in reversed(risks) if not r.risk.allow), None)
            if pick is None and risks:
                pick = risks[-1]
            if pick is not None:
                yield {"type": "risk", "payload": pick.model_dump()}
        elif channel == "book":
            yield {"type": "book", "payload": self.snapshot_book(symbol)}
        elif channel == "trades":
            with self._lock:
                recent = list(self._trades.get(symbol, [])[:8])
            for row in reversed(recent):
                yield {
                    "type": "trade",
                    "payload": {
                        "symbol": row["symbol"],
                        "ts": row["ts"],
                        "price": row["price"],
                        "qty": row["qty"],
                        "side": row["side"],
                        "phase": row.get("phase"),
                    },
                }

        while True:
            tick_n += 1
            now = int(time.time() * 1000)
            with self._lock:
                new_ticks = self._advance_locked(symbol, now)

            if channel == "candles":
                candles = self.get_candles(symbol, iv)
                if candles:
                    yield {"type": "candle", "payload": candles[-1].model_dump()}
                if tick_n % 3 == 0:
                    snap = self.get_pumpfun_snapshot(symbol)
                    if snap is not None:
                        yield {"type": "pumpfun_curve", "payload": snap.model_dump()}
                await asyncio.sleep(1.0)

            elif channel == "book":
                await asyncio.sleep(1.5)
                yield {"type": "book", "payload": self.snapshot_book(symbol)}

            elif channel == "trades":
                for tr in new_ticks:
                    yield {
                        "type": "trade",
                        "payload": {
                            "symbol": tr.symbol,
                            "ts": tr.ts,
                            "price": tr.price,
                            "qty": tr.qty,
                            "side": tr.side,
                            "phase": tr.phase,
                        },
                    }
                if tick_n % 2 == 0:
                    snap = self.get_pumpfun_snapshot(symbol)
                    if snap is not None:
                        yield {"type": "pumpfun_curve", "payload": snap.model_dump()}
                await asyncio.sleep(0.8)

            elif channel == "signals":
                await asyncio.sleep(20.0)
                candles = self.get_candles(symbol, iv)
                if not candles:
                    continue
                last = candles[-1]
                side = "long" if tick_n % 2 == 0 else "short"
                ev = SignalEvent(
                    strategyId="demo-momentum-v0",
                    symbol=symbol,
                    t=last.t,
                    signal=SignalOut(
                        side=side,  # type: ignore[arg-type]
                        strength=0.55 + (tick_n % 4) * 0.1,
                        reason=f"live_demo_{side}_{tick_n}",
                        tags=["demo", "live", "pumpfun_paper"],
                    ),
                )
                yield {"type": "signal", "payload": ev.model_dump()}

            elif channel == "fills":
                await asyncio.sleep(22.0)
                candles = self.get_candles(symbol, iv)
                if not candles:
                    continue
                last = candles[-1]
                qty = 60.0 if tick_n % 2 == 0 else -45.0
                fill = Fill(
                    ts=int(time.time() * 1000),
                    price=last.c,
                    qty=qty,
                    fee=abs(last.c * qty) * 0.0015,
                    slippage_bps=9.0,
                    tag="demo-momentum-v0",
                )
                yield {"type": "fill", "payload": fill.model_dump()}

            elif channel == "risk":
                await asyncio.sleep(25.0)
                c = self._curves.get(symbol)
                near = bool(c and (c.complete or progress_bps(c.real_token) >= 9500 or c.migrated))
                deny = tick_n % 3 == 0
                tags = []
                if deny:
                    tags = ["CURVE_NEAR_GRADUATION"] if near else ["COOLDOWN"]
                elif near:
                    tags = ["CURVE_NEAR_GRADUATION"]
                ev = RiskEvent(
                    strategyId="demo-momentum-v0",
                    symbol=symbol,
                    t=int(time.time() * 1000),
                    risk=RiskOut(
                        allow=not deny,
                        tags=tags,
                        notes="live pumpfun paper risk" if deny else "ok",
                    ),
                )
                yield {"type": "risk", "payload": ev.model_dump()}

            else:
                await asyncio.sleep(5.0)
