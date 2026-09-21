"""Deterministic mock MarketDataProvider + demo strategy feed."""
from __future__ import annotations

import asyncio
import hashlib
import math
import time
from typing import AsyncIterator

from app.models.contracts import (
    Candle,
    Fill,
    RiskEvent,
    RiskOut,
    SignalEvent,
    SignalOut,
    SymbolInfo,
)
from app.providers.base import MarketDataProvider

SYMBOLS: list[SymbolInfo] = [
    SymbolInfo(symbol="MOCK/USDC", base="MOCK", quote="USDC"),
    SymbolInfo(symbol="PEPEMOCK/SOL", base="PEPEMOCK", quote="SOL"),
    SymbolInfo(symbol="DOGEFAKE/USDC", base="DOGEFAKE", quote="USDC"),
    SymbolInfo(symbol="WIFMOCK/SOL", base="WIFMOCK", quote="SOL"),
    SymbolInfo(symbol="BONKFAKE/USDC", base="BONKFAKE", quote="USDC"),
]

INTERVAL_MS = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "1h": 3_600_000,
}


def _seed_int(s: str) -> int:
    return int(hashlib.sha256(s.encode()).hexdigest()[:8], 16)


class DetRNG:
    """Tiny LCG for reproducible series given a seed."""

    def __init__(self, seed: int):
        self.state = seed & 0xFFFFFFFF

    def next(self) -> float:
        self.state = (1664525 * self.state + 1013904223) & 0xFFFFFFFF
        return self.state / 0x100000000

    def uniform(self, a: float, b: float) -> float:
        return a + (b - a) * self.next()

    def gauss(self, mu: float = 0.0, sigma: float = 1.0) -> float:
        # Box-Muller
        u1 = max(self.next(), 1e-12)
        u2 = self.next()
        z = math.sqrt(-2.0 * math.log(u1)) * math.cos(2 * math.pi * u2)
        return mu + sigma * z


def _base_price(symbol: str) -> float:
    return {
        "MOCK/USDC": 1.25,
        "PEPEMOCK/SOL": 0.00042,
        "DOGEFAKE/USDC": 0.18,
        "WIFMOCK/SOL": 2.35,
        "BONKFAKE/USDC": 0.000031,
    }.get(symbol, 1.0)


class MockMarketDataProvider(MarketDataProvider):
    name = "mock"

    def __init__(self):
        self._history_bars = 180
        # Pre-generate signal/fill/risk history for REST
        self._signal_cache: dict[str, list[SignalEvent]] = {}
        self._fill_cache: dict[str, list[Fill]] = {}
        self._risk_cache: dict[str, list[RiskEvent]] = {}
        for sym in SYMBOLS:
            self._build_overlays(sym.symbol, "1m")

    def list_symbols(self) -> list[SymbolInfo]:
        return list(SYMBOLS)

    def get_candles(
        self, symbol: str, interval: str = "1m", from_ts: int | None = None, to_ts: int | None = None
    ) -> list[Candle]:
        if symbol not in {s.symbol for s in SYMBOLS}:
            return []
        iv = INTERVAL_MS.get(interval, 60_000)
        now = int(time.time() * 1000)
        # Align to interval
        end_t = (now // iv) * iv
        start_t = end_t - self._history_bars * iv
        if from_ts is not None:
            start_t = max(start_t, (from_ts // iv) * iv)
        if to_ts is not None:
            end_t = min(end_t, (to_ts // iv) * iv)

        rng = DetRNG(_seed_int(f"{symbol}:{interval}"))
        px = _base_price(symbol)
        # Warm-up walk so same absolute bar times are reproducible across calls
        # by seeding per-bar from symbol+interval+t
        candles: list[Candle] = []
        t = start_t
        prev_c = None
        while t <= end_t:
            bar_rng = DetRNG(_seed_int(f"{symbol}:{interval}:{t}"))
            if prev_c is None:
                # bootstrap from absolute seed walk of N steps before start
                boot = DetRNG(_seed_int(f"{symbol}:{interval}"))
                c0 = px
                for _ in range(50):
                    c0 *= 1 + boot.gauss(0, 0.008)
                prev_c = abs(c0)

            ret = bar_rng.gauss(0.0005, 0.012)
            o = prev_c
            c = max(o * (1 + ret), px * 1e-6)
            wiggle = abs(bar_rng.gauss(0, 0.006))
            h = max(o, c) * (1 + wiggle)
            l = min(o, c) * (1 - wiggle)
            v = abs(bar_rng.gauss(5e5, 2e5))
            candles.append(
                Candle(symbol=symbol, interval=interval, t=t, o=o, h=h, l=l, c=c, v=v)
            )
            prev_c = c
            t += iv
        return candles

    def _build_overlays(self, symbol: str, interval: str = "1m") -> None:
        candles = self.get_candles(symbol, interval)
        if len(candles) < 20:
            self._signal_cache[symbol] = []
            self._fill_cache[symbol] = []
            self._risk_cache[symbol] = []
            return

        sigs: list[SignalEvent] = []
        fills: list[Fill] = []
        risks: list[RiskEvent] = []
        # Every N bars emit long/short; ensure at least one of each
        n = 18
        sides = ["long", "short"]
        for i, c in enumerate(candles):
            if i < 10 or i % n != 0:
                continue
            side = sides[(i // n) % 2]
            strength = 0.45 + ((i // n) % 5) * 0.1
            reason = f"demo_{side}_momentum_bar{i}"
            sig = SignalOut(side=side, strength=min(strength, 0.95), reason=reason, tags=["demo"])
            ev = SignalEvent(strategyId="demo-momentum-v0", symbol=symbol, t=c.t, signal=sig)
            sigs.append(ev)

            # Risk: every M-th signal deny
            m = 4
            deny = ((i // n) % m) == (m - 1)
            if deny:
                risk = RiskOut(
                    allow=False,
                    tags=["SPREAD_TOO_WIDE" if (i // n) % 2 == 0 else "COOLDOWN"],
                    notes="mock risk deny",
                )
                risks.append(
                    RiskEvent(strategyId="demo-momentum-v0", symbol=symbol, t=c.t, risk=risk)
                )
                # no fill when denied
                continue

            risks.append(
                RiskEvent(
                    strategyId="demo-momentum-v0",
                    symbol=symbol,
                    t=c.t,
                    risk=RiskOut(allow=True, tags=[], notes="ok"),
                )
            )
            # Fill 1–2 bars later (paper latency)
            delay = 1 + (i // n) % 2
            if i + delay < len(candles):
                fc = candles[i + delay]
                qty = 100.0 if side == "long" else -80.0
                slip = 8.0 + (i % 7)
                fills.append(
                    Fill(
                        ts=fc.t,
                        price=fc.c,
                        qty=qty,
                        fee=abs(fc.c * qty) * 0.0015,
                        slippage_bps=slip,
                        tag="demo-momentum-v0",
                    )
                )

        # Guarantee ≥1 long and ≥1 short even on short series
        if not any(s.signal.side == "long" for s in sigs) and candles:
            c = candles[max(12, len(candles) // 3)]
            sigs.append(
                SignalEvent(
                    strategyId="demo-momentum-v0",
                    symbol=symbol,
                    t=c.t,
                    signal=SignalOut(side="long", strength=0.7, reason="seed_long"),
                )
            )
            fills.append(
                Fill(ts=c.t + INTERVAL_MS[interval], price=c.c, qty=50.0, fee=0.01, slippage_bps=10.0, tag="demo")
            )
        if not any(s.signal.side == "short" for s in sigs) and candles:
            c = candles[max(20, 2 * len(candles) // 3)]
            sigs.append(
                SignalEvent(
                    strategyId="demo-momentum-v0",
                    symbol=symbol,
                    t=c.t,
                    signal=SignalOut(side="short", strength=0.65, reason="seed_short"),
                )
            )
            fills.append(
                Fill(ts=c.t + INTERVAL_MS[interval], price=c.c, qty=-40.0, fee=0.01, slippage_bps=12.0, tag="demo")
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

    def snapshot_book(self, symbol: str) -> dict:
        candles = self.get_candles(symbol, "1m")
        mid = candles[-1].c if candles else _base_price(symbol)
        rng = DetRNG(_seed_int(f"book:{symbol}:{int(time.time()) // 5}"))
        spread_bps = 15 + rng.uniform(0, 40)
        half = mid * (spread_bps / 1e4) / 2
        bids, asks = [], []
        for i in range(10):
            step = half * (1 + i * 0.35)
            sz = abs(rng.gauss(800, 200)) * (1 + i * 0.1)
            bids.append({"price": mid - half - step, "size": sz})
            asks.append({"price": mid + half + step, "size": sz})
        return {"symbol": symbol, "bids": bids, "asks": asks, "mid": mid, "spread_bps": spread_bps}

    async def stream(
        self, channel: str, symbol: str, interval: str | None = None
    ) -> AsyncIterator[dict]:
        """Async generator yielding push frames for one subscription."""
        iv = interval or "1m"
        tick = 0

        # Immediate risk/book snapshot (signals+fills come from REST history)
        if channel == "risk":
            risks = self.get_risk_events(symbol)
            # Prefer last deny so RiskTagBar shows tags; else last event
            pick = next((r for r in reversed(risks) if not r.risk.allow), None)
            if pick is None and risks:
                pick = risks[-1]
            if pick is not None:
                yield {"type": "risk", "payload": pick.model_dump()}
            else:
                yield {
                    "type": "risk",
                    "payload": RiskEvent(
                        strategyId="demo-momentum-v0",
                        symbol=symbol,
                        t=int(time.time() * 1000),
                        risk=RiskOut(allow=False, tags=["SPREAD_TOO_WIDE"], notes="seed deny"),
                    ).model_dump(),
                }
        elif channel == "book":
            yield {"type": "book", "payload": self.snapshot_book(symbol)}

        while True:
            tick += 1
            if channel == "candles":
                candles = self.get_candles(symbol, iv)
                if candles:
                    last = candles[-1].model_dump()
                    rng = DetRNG(_seed_int(f"live:{symbol}:{iv}:{tick}"))
                    last["c"] = last["c"] * (1 + rng.gauss(0, 0.0015))
                    last["h"] = max(last["h"], last["c"])
                    last["l"] = min(last["l"], last["c"])
                    last["v"] = last["v"] + abs(rng.gauss(1e3, 400))
                    yield {"type": "candle", "payload": last}
                await asyncio.sleep(1.0)

            elif channel == "book":
                await asyncio.sleep(1.5)
                yield {"type": "book", "payload": self.snapshot_book(symbol)}

            elif channel == "trades":
                candles = self.get_candles(symbol, iv)
                mid = candles[-1].c if candles else _base_price(symbol)
                rng = DetRNG(_seed_int(f"trade:{symbol}:{tick}:{int(time.time()*10)}"))
                if rng.next() > 0.35:
                    side = "buy" if rng.next() > 0.5 else "sell"
                    px = mid * (1 + rng.gauss(0, 0.0008))
                    qty = abs(rng.gauss(120, 40))
                    yield {
                        "type": "trade",
                        "payload": {
                            "symbol": symbol,
                            "ts": int(time.time() * 1000),
                            "price": px,
                            "qty": qty,
                            "side": side,
                        },
                    }
                await asyncio.sleep(0.8)

            elif channel == "signals":
                await asyncio.sleep(20.0)
                candles = self.get_candles(symbol, iv)
                if not candles:
                    continue
                last = candles[-1]
                side = "long" if tick % 2 == 0 else "short"
                ev = SignalEvent(
                    strategyId="demo-momentum-v0",
                    symbol=symbol,
                    t=last.t,
                    signal=SignalOut(
                        side=side,
                        strength=0.55 + (tick % 4) * 0.1,
                        reason=f"live_demo_{side}_{tick}",
                        tags=["demo", "live"],
                    ),
                )
                yield {"type": "signal", "payload": ev.model_dump()}

            elif channel == "fills":
                await asyncio.sleep(22.0)
                candles = self.get_candles(symbol, iv)
                if not candles:
                    continue
                last = candles[-1]
                qty = 60.0 if tick % 2 == 0 else -45.0
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
                deny = tick % 3 == 0
                risk = RiskOut(
                    allow=not deny,
                    tags=["COOLDOWN"] if deny else (["POSITION_CAP"] if tick % 5 == 0 else []),
                    notes="live mock risk" if deny else "ok",
                )
                ev = RiskEvent(
                    strategyId="demo-momentum-v0",
                    symbol=symbol,
                    t=int(time.time() * 1000),
                    risk=risk,
                )
                yield {"type": "risk", "payload": ev.model_dump()}

            else:
                await asyncio.sleep(5.0)
