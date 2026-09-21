"""TraderSnapshotter — mock-first holdings + recent buys/sells (distill-v0 fields)."""
from __future__ import annotations

from statistics import median
from typing import Optional, Protocol

from app.models.contracts import (
    TradeBrief,
    TraderPosition,
    TraderSnapshot,
    TraderWatchlistItem,
)
from app.traders.store import WATCH_GRAD, WATCH_MID, WATCH_SNIPER, get_watch

PROGRESS_HIST_KEYS = (
    "0_800",
    "800_5000",
    "5000_7500",
    "7500_9000",
    "9000_10000",
    "migrated",
)

MOCK_MINT_A = "MockMintAlpha11111111111111111111111111111"
MOCK_MINT_B = "MockMintBravo11111111111111111111111111111"
MOCK_MINT_C = "MockMintCharlie111111111111111111111111111"


def empty_progress_hist() -> dict[str, int]:
    return {k: 0 for k in PROGRESS_HIST_KEYS}


def progress_bucket(progress_bps: Optional[int], *, migrated: bool = False) -> str:
    if migrated:
        return "migrated"
    if progress_bps is None:
        return "800_5000"
    if progress_bps < 800:
        return "0_800"
    if progress_bps < 5000:
        return "800_5000"
    if progress_bps < 7500:
        return "5000_7500"
    if progress_bps < 9000:
        return "7500_9000"
    return "9000_10000"


def build_progress_hist(
    positions: list[TraderPosition], buys: list[TradeBrief]
) -> dict[str, int]:
    hist = empty_progress_hist()
    seen: set[str] = set()
    for pos in positions:
        key = progress_bucket(pos.progress_bps, migrated=pos.phase == "amm")
        hist[key] = hist.get(key, 0) + 1
        seen.add(pos.mint)
    for buy in buys:
        if buy.mint in seen:
            continue
        key = progress_bucket(buy.progress_bps)
        hist[key] = hist.get(key, 0) + 1
        seen.add(buy.mint)
    return hist


def _brief(
    ts: int,
    mint: str,
    side: str,
    sol_amount: float,
    progress_bps: int,
) -> TradeBrief:
    return TradeBrief(
        ts=ts,
        mint=mint,
        side=side,  # type: ignore[arg-type]
        sol_amount=sol_amount,
        progress_bps=progress_bps,
        signature=None,
    )


def _median_int(values: list[int]) -> Optional[int]:
    if not values:
        return None
    return int(median(values))


class TraderReader(Protocol):
    name: str

    def fetch_snapshot(
        self, item: TraderWatchlistItem, *, now_ms: Optional[int] = None
    ) -> TraderSnapshot: ...


def mock_snapshot_for(
    item: TraderWatchlistItem, *, now_ms: Optional[int] = None, persona: Optional[str] = None
) -> TraderSnapshot:
    """Deterministic mock holdings + tape matching distill-v0 TraderSnapshot fields."""
    ts = int(now_ms if now_ms is not None else 1_700_000_000_000)
    kind = persona or _persona_for(item)
    if kind == "sniper":
        return _sniper_snapshot(item, ts)
    if kind == "graduation_chase":
        return _grad_snapshot(item, ts)
    if kind == "flip":
        return _flip_snapshot(item, ts)
    if kind == "bag":
        return _bag_snapshot(item, ts)
    return _mid_snapshot(item, ts)


def _persona_for(item: TraderWatchlistItem) -> str:
    if item.watch_id == "watch-sniper" or item.address == WATCH_SNIPER:
        return "sniper"
    if item.watch_id == "watch-grad" or item.address == WATCH_GRAD:
        return "graduation_chase"
    if item.watch_id == "watch-mid" or item.address == WATCH_MID:
        return "mid_curve"
    label = (item.label or "").lower()
    for name in ("sniper", "graduation_chase", "grad", "flip", "bag", "mid"):
        if name in label:
            if name == "grad":
                return "graduation_chase"
            if name == "mid":
                return "mid_curve"
            return name
    return "mid_curve"


def _sniper_snapshot(item: TraderWatchlistItem, ts: int) -> TraderSnapshot:
    entries = [120, 200, 280, 350, 420, 510, 640, 720]
    buys = [
        _brief(ts - (i + 1) * 90_000, MOCK_MINT_A if i % 2 == 0 else MOCK_MINT_B, "buy", 0.08, p)
        for i, p in enumerate(entries)
    ]
    sells = [
        _brief(ts - 40_000, MOCK_MINT_B, "sell", 0.07, 500),
        _brief(ts - 20_000, MOCK_MINT_A, "sell", 0.06, 600),
    ]
    positions = [
        TraderPosition(
            mint=MOCK_MINT_A,
            symbol="SNIP/SOL",
            qty=12_000,
            cost_basis_sol=0.08,
            unrealized_pnl_sol=0.01,
            hold_sec=45.0,
            progress_bps=400,
            phase="curve",
        )
    ]
    return _assemble(
        item,
        ts,
        positions,
        buys,
        sells,
        median_hold_sec_24h=45.0,
        flip_rate_24h=0.35,
        slot=101,
    )


def _mid_snapshot(item: TraderWatchlistItem, ts: int) -> TraderSnapshot:
    entries = [1200, 1500, 1800, 2200, 2500, 2800, 3200, 4000, 4500, 5000]
    buys = [
        _brief(
            ts - (i + 1) * 180_000,
            MOCK_MINT_A if i < 5 else MOCK_MINT_B,
            "buy",
            0.15,
            p,
        )
        for i, p in enumerate(entries)
    ]
    sells = [
        _brief(ts - 50_000, MOCK_MINT_A, "sell", 0.12, 3600),
    ]
    positions = [
        TraderPosition(
            mint=MOCK_MINT_B,
            symbol="MID/SOL",
            qty=8_000,
            cost_basis_sol=0.20,
            unrealized_pnl_sol=0.03,
            hold_sec=480.0,
            progress_bps=4100,
            phase="curve",
        ),
        TraderPosition(
            mint=MOCK_MINT_C,
            symbol="MID2/SOL",
            qty=3_000,
            cost_basis_sol=0.10,
            unrealized_pnl_sol=0.01,
            hold_sec=520.0,
            progress_bps=3300,
            phase="curve",
        ),
    ]
    return _assemble(
        item,
        ts,
        positions,
        buys,
        sells,
        median_hold_sec_24h=500.0,
        flip_rate_24h=0.22,
        slot=202,
    )


def _grad_snapshot(item: TraderWatchlistItem, ts: int) -> TraderSnapshot:
    entries = [9100, 9200, 9300, 9400, 9500, 9650]
    buys = [
        _brief(ts - (i + 1) * 120_000, MOCK_MINT_C, "buy", 0.22, p)
        for i, p in enumerate(entries)
    ]
    sells = [
        _brief(ts - 30_000, MOCK_MINT_C, "sell", 0.18, 9800),
    ]
    positions = [
        TraderPosition(
            mint=MOCK_MINT_C,
            symbol="GRAD/SOL",
            qty=4_000,
            cost_basis_sol=0.25,
            unrealized_pnl_sol=-0.04,
            hold_sec=420.0,
            progress_bps=9400,
            phase="graduating",
        )
    ]
    return _assemble(
        item,
        ts,
        positions,
        buys,
        sells,
        median_hold_sec_24h=420.0,
        flip_rate_24h=0.18,
        slot=303,
    )


def _flip_snapshot(item: TraderWatchlistItem, ts: int) -> TraderSnapshot:
    # Mixed entries so sniper/mid/grad thresholds miss; short hold + high flip.
    entries = [500, 500, 500, 2000, 2000, 2000, 2000, 8500, 8500, 8500]
    buys = [
        _brief(ts - (i + 1) * 40_000, MOCK_MINT_A, "buy", 0.05, p)
        for i, p in enumerate(entries)
    ]
    sells = [
        _brief(ts - (i + 1) * 20_000, MOCK_MINT_A, "sell", 0.05, p)
        for i, p in enumerate(entries[:8])
    ]
    return _assemble(
        item,
        ts,
        [],
        buys,
        sells,
        median_hold_sec_24h=90.0,
        flip_rate_24h=0.80,
        slot=404,
    )


def _bag_snapshot(item: TraderWatchlistItem, ts: int) -> TraderSnapshot:
    buys = [
        _brief(ts - 8 * 3_600_000, MOCK_MINT_B, "buy", 0.40, 200),
        _brief(ts - 6 * 3_600_000, MOCK_MINT_C, "buy", 0.35, 250),
    ]
    positions = [
        TraderPosition(
            mint=MOCK_MINT_B,
            symbol="BAG/SOL",
            qty=50_000,
            cost_basis_sol=0.40,
            unrealized_pnl_sol=-0.12,
            hold_sec=8_000.0,
            progress_bps=2700,
            phase="curve",
        ),
        TraderPosition(
            mint=MOCK_MINT_C,
            symbol="BAG2/SOL",
            qty=20_000,
            cost_basis_sol=0.35,
            unrealized_pnl_sol=-0.08,
            hold_sec=7_200.0,
            progress_bps=3100,
            phase="curve",
        ),
    ]
    return _assemble(
        item,
        ts,
        positions,
        buys,
        [],
        median_hold_sec_24h=7_600.0,
        flip_rate_24h=0.05,
        slot=505,
    )


def _assemble(
    item: TraderWatchlistItem,
    ts: int,
    positions: list[TraderPosition],
    buys: list[TradeBrief],
    sells: list[TradeBrief],
    *,
    median_hold_sec_24h: Optional[float],
    flip_rate_24h: Optional[float],
    slot: Optional[int],
) -> TraderSnapshot:
    hour = 3_600_000
    buys_1h = [b for b in buys if ts - b.ts <= hour]
    sells_1h = [s for s in sells if ts - s.ts <= hour]
    entry_bps = [int(b.progress_bps) for b in buys if b.progress_bps is not None]
    return TraderSnapshot(
        watch_id=item.watch_id,
        address=item.address,
        asof_ts=ts,
        slot=slot,
        positions=positions,
        open_count=len(positions),
        gross_exposure_sol=round(
            sum(max(p.cost_basis_sol or 0.0, 0.0) + max(p.unrealized_pnl_sol or 0.0, 0.0) for p in positions),
            6,
        ),
        recent_buys=buys,
        recent_sells=sells,
        buy_notional_1h=round(sum(b.sol_amount for b in buys_1h), 6),
        sell_notional_1h=round(sum(s.sol_amount for s in sells_1h), 6),
        trade_count_1h=len(buys_1h) + len(sells_1h),
        median_hold_sec_24h=median_hold_sec_24h,
        flip_rate_24h=flip_rate_24h,
        progress_hist=build_progress_hist(positions, buys),
        entry_progress_median_bps=_median_int(entry_bps),
    )


class MockTraderReader:
    name = "mock"

    def fetch_snapshot(
        self, item: TraderWatchlistItem, *, now_ms: Optional[int] = None
    ) -> TraderSnapshot:
        return mock_snapshot_for(item, now_ms=now_ms)


def get_reader() -> TraderReader:
    from app.traders.helius import HeliusTraderReader, reader_mode

    mode = reader_mode()
    if mode == "helius":
        return HeliusTraderReader()
    return MockTraderReader()


def get_snapshot(
    watch_id_or_address: str, *, now_ms: Optional[int] = None
) -> Optional[TraderSnapshot]:
    item = get_watch(watch_id_or_address)
    if item is None:
        return None
    return get_reader().fetch_snapshot(item, now_ms=now_ms)
