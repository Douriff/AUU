"""PaperTradeJournal — FIFO round-trips + PaperStats (+ optional trades-MC)."""
from __future__ import annotations

import json
import os
import random
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional

from app.models.contracts import Fill
from app.providers.pumpfun_curve_math import split_impact_gross_fee_net

DEFAULT_MC_PATHS = 500
DEFAULT_MC_SEED = 42
MIN_SAMPLE_OK = 20
JOURNAL_N = 50
EQUITY_0 = 10_000.0
DISCLAIMER = (
    "simulation from paper history, not a promise — 纸面历史重抽样，非实盘承诺"
)

_REASON_TAGS = {
    "take_profit": "TAKE_PROFIT",
    "stop_loss": "STOP_LOSS",
    "graduation": "CURVE_NEAR_GRADUATION",
    "sell_pressure": "SELL_PRESSURE",
    "impact_split": "SPLIT_REDUCE",
    "max_hold": "MAX_HOLD",
}


@dataclass
class OpenLot:
    symbol: str
    qty: float  # signed: +long / -short remaining
    price: float
    ts: int
    fees: float
    tag: str = ""
    mint: Optional[str] = None
    strategy_id: str = "manual-paper"
    estimated_impact_bps: Optional[float] = None
    estimated_impact_gross_bps: Optional[float] = None
    estimated_impact_net_bps: Optional[float] = None
    protocol_fee_bps: Optional[float] = None
    quote_price: Optional[float] = None
    shadow_slippage_bps: Optional[float] = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "qty": self.qty,
            "price": self.price,
            "ts": self.ts,
            "fees": self.fees,
            "tag": self.tag,
            "mint": self.mint,
            "strategy_id": self.strategy_id,
            "estimated_impact_bps": self.estimated_impact_bps,
            "estimated_impact_gross_bps": self.estimated_impact_gross_bps,
            "estimated_impact_net_bps": self.estimated_impact_net_bps,
            "protocol_fee_bps": self.protocol_fee_bps,
            "quote_price": self.quote_price,
            "shadow_slippage_bps": self.shadow_slippage_bps,
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "OpenLot":
        return cls(
            symbol=str(d.get("symbol") or ""),
            qty=float(d.get("qty") or 0.0),
            price=float(d.get("price") or 0.0),
            ts=int(d.get("ts") or 0),
            fees=float(d.get("fees") or 0.0),
            tag=str(d.get("tag") or ""),
            mint=d.get("mint"),
            strategy_id=str(d.get("strategy_id") or "manual-paper"),
            estimated_impact_bps=_opt_float(d.get("estimated_impact_bps")),
            estimated_impact_gross_bps=_opt_float(d.get("estimated_impact_gross_bps")),
            estimated_impact_net_bps=_opt_float(d.get("estimated_impact_net_bps")),
            protocol_fee_bps=_opt_float(d.get("protocol_fee_bps")),
            quote_price=_opt_float(d.get("quote_price")),
            shadow_slippage_bps=_opt_float(d.get("shadow_slippage_bps")),
        )


def _opt_float(v: Any) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def impact_triple_from_mapping(row: Mapping[str, Any]) -> tuple[Optional[float], Optional[float], Optional[float]]:
    """Gross / protocol fee / net already stored on a fill or lot. No invented impact."""
    gross = _opt_float(row.get("estimated_impact_gross_bps"))
    if gross is None:
        gross = _opt_float(row.get("estimated_impact_bps"))
    if gross is None:
        gross = _opt_float(row.get("entry_estimated_impact_gross_bps"))
    if gross is None:
        gross = _opt_float(row.get("entry_estimated_impact_bps"))
    if gross is None:
        return None, None, None
    fee = _opt_float(row.get("protocol_fee_bps"))
    if fee is None:
        fee = _opt_float(row.get("entry_protocol_fee_bps"))
    net = _opt_float(row.get("estimated_impact_net_bps"))
    if net is None:
        net = _opt_float(row.get("entry_estimated_impact_net_bps"))
    return split_impact_gross_fee_net(
        gross,
        protocol_fee_bps=fee,
        net_bps=net,
        phase=str(row.get("phase") or row.get("entry_phase") or "curve"),
    )


def impact_triple_from_fill(fill: Fill) -> tuple[Optional[float], Optional[float], Optional[float]]:
    return impact_triple_from_mapping(fill.model_dump())


@dataclass
class RoundTrip:
    """Closed paper round-trip. Manual Trade and autopaper share this journal."""

    id: str
    strategy_id: str  # pump-paper-v1 | manual-paper
    symbol: str
    mint: Optional[str]
    entry_ts: int
    exit_ts: int
    entry_price: float
    exit_price: float
    qty: float
    pnl: float
    pnl_pct: float
    fees: float
    tags: list[str] = field(default_factory=list)
    source: str = "manual"  # signal | manual  (never live; live fills use LiveTradeJournal)
    side: str = "long"
    entry_estimated_impact_bps: Optional[float] = None
    entry_estimated_impact_gross_bps: Optional[float] = None
    entry_estimated_impact_net_bps: Optional[float] = None
    entry_protocol_fee_bps: Optional[float] = None
    entry_quote_price: Optional[float] = None
    entry_shadow_slippage_bps: Optional[float] = None
    exit_estimated_impact_bps: Optional[float] = None
    exit_quote_price: Optional[float] = None
    exit_shadow_slippage_bps: Optional[float] = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "strategy_id": self.strategy_id,
            "symbol": self.symbol,
            "mint": self.mint,
            "entry_ts": self.entry_ts,
            "exit_ts": self.exit_ts,
            "entry_price": self.entry_price,
            "exit_price": self.exit_price,
            "qty": self.qty,
            "pnl": self.pnl,
            "pnl_pct": self.pnl_pct,
            "fees": self.fees,
            "tags": list(self.tags),
            "source": self.source,
            "side": self.side,
            "entry_estimated_impact_bps": self.entry_estimated_impact_bps,
            "entry_estimated_impact_gross_bps": self.entry_estimated_impact_gross_bps,
            "entry_estimated_impact_net_bps": self.entry_estimated_impact_net_bps,
            "entry_protocol_fee_bps": self.entry_protocol_fee_bps,
            "entry_quote_price": self.entry_quote_price,
            "entry_shadow_slippage_bps": self.entry_shadow_slippage_bps,
            "exit_estimated_impact_bps": self.exit_estimated_impact_bps,
            "exit_quote_price": self.exit_quote_price,
            "exit_shadow_slippage_bps": self.exit_shadow_slippage_bps,
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "RoundTrip":
        tags = d.get("tags") or []
        if not isinstance(tags, list):
            tags = []
        return cls(
            id=str(d.get("id") or uuid.uuid4()),
            strategy_id=str(d.get("strategy_id") or "manual-paper"),
            symbol=str(d.get("symbol") or ""),
            mint=d.get("mint"),
            entry_ts=int(d.get("entry_ts") or 0),
            exit_ts=int(d.get("exit_ts") or 0),
            entry_price=float(d.get("entry_price") or 0.0),
            exit_price=float(d.get("exit_price") or 0.0),
            qty=float(d.get("qty") or 0.0),
            pnl=float(d.get("pnl") or 0.0),
            pnl_pct=float(d.get("pnl_pct") or 0.0),
            fees=float(d.get("fees") or 0.0),
            tags=[str(t) for t in tags],
            source=str(d.get("source") or "manual"),
            side=str(d.get("side") or "long"),
            entry_estimated_impact_bps=_opt_float(d.get("entry_estimated_impact_bps")),
            entry_estimated_impact_gross_bps=_opt_float(d.get("entry_estimated_impact_gross_bps")),
            entry_estimated_impact_net_bps=_opt_float(d.get("entry_estimated_impact_net_bps")),
            entry_protocol_fee_bps=_opt_float(d.get("entry_protocol_fee_bps")),
            entry_quote_price=_opt_float(d.get("entry_quote_price")),
            entry_shadow_slippage_bps=_opt_float(d.get("entry_shadow_slippage_bps")),
            exit_estimated_impact_bps=_opt_float(d.get("exit_estimated_impact_bps")),
            exit_quote_price=_opt_float(d.get("exit_quote_price")),
            exit_shadow_slippage_bps=_opt_float(d.get("exit_shadow_slippage_bps")),
        )


ClosedTrade = RoundTrip


def _is_live_tag(tag: str) -> bool:
    t = (tag or "").lower()
    if t.startswith("live") or ":live" in t or "venue:live" in t:
        return True
    if "source=live" in t or "source:live" in t:
        return True
    return False


def _ids_from_tag(tag: str) -> tuple[str, str]:
    t = (tag or "").lower()
    if _is_live_tag(t):
        # Live fills belong on the live ledger — paper journal never claims them.
        return "live", "live"
    if "pump-paper-v1" in t or "autopaper" in t:
        return "pump-paper-v1", "signal"
    return "manual-paper", "manual"


def _tags_for(reason: str, tag: str) -> list[str]:
    out: list[str] = []
    mapped = _REASON_TAGS.get((reason or "").lower())
    if mapped:
        out.append(mapped)
    elif reason:
        out.append(reason)
    if tag and tag not in out:
        out.append(tag)
    return out


class PaperTradeJournal:
    """FIFO round-trip matcher for PaperBroker fills (persists closed trades)."""

    def __init__(self, equity_0: float = EQUITY_0) -> None:
        self.equity_0 = float(equity_0)
        self.fills: list[dict[str, Any]] = []
        self.lots: dict[str, list[OpenLot]] = {}
        self.closed: list[RoundTrip] = []
        self.persist_path: Optional[Path] = None

    def reset(self) -> None:
        self.fills.clear()
        self.lots.clear()
        self.closed.clear()
        self._maybe_persist()

    def record_fill(
        self,
        symbol: str,
        fill: Fill,
        *,
        reason: str = "",
        mint: Optional[str] = None,
    ) -> list[RoundTrip]:
        qty = float(fill.qty)
        px = float(fill.price)
        fee = float(fill.fee or 0.0)
        ts = int(fill.ts)
        tag = fill.tag or ""
        if _is_live_tag(tag):
            # PaperTradeJournal / stats win-rate stay paper-only.
            return []
        strategy_id, source = _ids_from_tag(tag)
        if source == "live":
            return []
        if qty == 0 or px <= 0:
            return []
        dumped = fill.model_dump()
        dumped["symbol"] = symbol
        self.fills.append(dumped)
        opened = self.lots.setdefault(symbol, [])
        remaining = qty
        new_closed: list[RoundTrip] = []

        i = 0
        while remaining != 0 and i < len(opened):
            lot = opened[i]
            if lot.qty * remaining > 0:
                i += 1
                continue
            take = min(abs(lot.qty), abs(remaining))
            lot_sign = 1.0 if lot.qty > 0 else -1.0
            close_qty = take * lot_sign
            fee_share = 0.0
            if abs(lot.qty) > 0:
                fee_share += lot.fees * (take / abs(lot.qty))
            if abs(qty) > 0:
                fee_share += fee * (take / abs(qty))
            if lot.qty > 0:
                side = "long"
                pnl = (px - lot.price) * take - fee_share
                pnl_pct = (px - lot.price) / lot.price
            else:
                side = "short"
                pnl = (lot.price - px) * take - fee_share
                pnl_pct = (lot.price - px) / lot.price
            if lot.price * take > 0:
                pnl_pct -= fee_share / (lot.price * take)
            src_tag = tag or lot.tag
            sid, src = _ids_from_tag(src_tag or lot.strategy_id)
            entry_gross, entry_fee, entry_net = impact_triple_from_mapping(lot.as_dict())
            trade = RoundTrip(
                id=str(uuid.uuid4()),
                strategy_id=sid,
                symbol=symbol,
                mint=mint or lot.mint,
                entry_ts=lot.ts,
                exit_ts=ts,
                entry_price=lot.price,
                exit_price=px,
                qty=take,
                pnl=pnl,
                pnl_pct=pnl_pct,
                fees=fee_share,
                tags=_tags_for(reason, src_tag),
                source=src,
                side=side,
                entry_estimated_impact_bps=entry_gross,
                entry_estimated_impact_gross_bps=entry_gross,
                entry_estimated_impact_net_bps=entry_net,
                entry_protocol_fee_bps=entry_fee,
                entry_quote_price=lot.quote_price,
                entry_shadow_slippage_bps=lot.shadow_slippage_bps,
                exit_estimated_impact_bps=getattr(fill, "estimated_impact_bps", None),
                exit_quote_price=getattr(fill, "quote_price", None),
                exit_shadow_slippage_bps=getattr(fill, "shadow_slippage_bps", None),
            )
            self.closed.append(trade)
            new_closed.append(trade)
            lot.qty -= close_qty
            remaining -= -close_qty
            if abs(lot.qty) <= 1e-12:
                lot.fees = 0.0
                opened.pop(i)
            else:
                lot.fees = max(0.0, lot.fees - fee_share)
                i += 1

        if abs(remaining) > 1e-12:
            leftover_fee = fee * (abs(remaining) / abs(qty)) if qty else 0.0
            gross, proto, net = impact_triple_from_fill(fill)
            opened.append(
                OpenLot(
                    symbol=symbol,
                    qty=remaining,
                    price=px,
                    ts=ts,
                    fees=leftover_fee,
                    tag=tag,
                    mint=mint,
                    strategy_id=strategy_id,
                    estimated_impact_bps=gross,
                    estimated_impact_gross_bps=gross,
                    estimated_impact_net_bps=net,
                    protocol_fee_bps=proto,
                    quote_price=getattr(fill, "quote_price", None),
                    shadow_slippage_bps=getattr(fill, "shadow_slippage_bps", None),
                )
            )
        if not opened:
            self.lots.pop(symbol, None)
        self._maybe_persist()
        return new_closed

    def _maybe_persist(self) -> None:
        if self.persist_path is None:
            return
        _write_journal(self.persist_path, self)

    def closed_in_window(
        self,
        *,
        window: Optional[int] = None,
        from_ts: Optional[int] = None,
        to_ts: Optional[int] = None,
    ) -> list[RoundTrip]:
        rows = [t for t in self.closed if (t.source or "") != "live"]
        if from_ts is not None:
            rows = [t for t in rows if t.exit_ts >= from_ts]
        if to_ts is not None:
            rows = [t for t in rows if t.exit_ts <= to_ts]
        if window is not None and window > 0:
            rows = rows[-window:]
        return rows


def _equity_curve(trades: list[RoundTrip], equity_0: float = EQUITY_0) -> list[dict[str, Any]]:
    eq = float(equity_0)
    if not trades:
        return []
    out = [{"t": trades[0].entry_ts, "equity": eq}]
    for t in trades:
        eq += t.pnl
        out.append({"t": t.exit_ts, "equity": eq})
    return out


def _max_drawdown_pct(equity: list[dict[str, Any]]) -> Optional[float]:
    if not equity:
        return None
    peak = equity[0]["equity"]
    max_dd = 0.0
    for pt in equity:
        val = float(pt["equity"])
        if val > peak:
            peak = val
        if peak > 0:
            dd = (peak - val) / peak
            if dd > max_dd:
                max_dd = dd
    return max_dd * 100.0


def _pctile(sorted_vals: list[float], p: float) -> float:
    if not sorted_vals:
        return 0.0
    idx = min(len(sorted_vals) - 1, max(0, int(round((p / 100.0) * (len(sorted_vals) - 1)))))
    return sorted_vals[idx]


def monte_carlo(
    pnls: list[float],
    *,
    n_paths: int = DEFAULT_MC_PATHS,
    seed: int = DEFAULT_MC_SEED,
    method: str = "shuffle",
    equity_0: float = EQUITY_0,
) -> dict[str, Any]:
    """P0 trades-MC: shuffle (no replacement) or bootstrap pnl, rebuild additive equity."""
    n = len(pnls)
    sample_ok = n >= MIN_SAMPLE_OK
    method = "bootstrap" if method in {"resample", "bootstrap"} else "shuffle"
    base: dict[str, Any] = {
        "n_paths": int(n_paths),
        "method": method,
        "seed": seed,
        "sample_ok": sample_ok,
        "label": DISCLAIMER,
    }
    if n == 0:
        base["note"] = "样本不足"
        return base
    rng = random.Random(seed)
    n_paths = max(1, int(n_paths))
    totals: list[float] = []
    dds: list[float] = []
    for _ in range(n_paths):
        if method == "shuffle":
            seq = list(pnls)
            rng.shuffle(seq)
        else:
            seq = [pnls[rng.randrange(n)] for _j in range(n)]
        eq = float(equity_0)
        peak = eq
        max_dd = 0.0
        for p in seq:
            eq += p
            if eq > peak:
                peak = eq
            if peak > 0:
                dd = (peak - eq) / peak
                if dd > max_dd:
                    max_dd = dd
        totals.append(eq - float(equity_0))
        dds.append(max_dd * 100.0)
    totals.sort()
    dds.sort()
    base.update(
        {
            "p05_pnl": _pctile(totals, 5),
            "p50_pnl": _pctile(totals, 50),
            "p95_pnl": _pctile(totals, 95),
            "p05_dd": _pctile(dds, 5),
            "p50_dd": _pctile(dds, 50),
        }
    )
    if not sample_ok:
        base["note"] = "样本不足"
    return base


def summarize(
    trades: list[RoundTrip],
    *,
    n_paths: int = DEFAULT_MC_PATHS,
    seed: int = DEFAULT_MC_SEED,
    window: str | int = "session",
    mc: bool = False,
    mc_method: str = "shuffle",
    equity_0: float = EQUITY_0,
) -> dict[str, Any]:
    # Win rate / expectancy / drawdown from journal trades only (QuantStats is idea-only).
    # Live fills (source=live) never mix into paper win-rate.
    trades = [t for t in trades if (t.source or "") != "live"]
    n = len(trades)
    wins = sum(1 for t in trades if t.pnl > 0)
    losses = sum(1 for t in trades if t.pnl < 0)
    pnls = [t.pnl for t in trades]
    win_rate = (wins / n) if n else None
    expectancy = (sum(pnls) / n) if n else None
    equity = _equity_curve(trades, equity_0)
    sample_ok = n >= MIN_SAMPLE_OK
    mc_payload = None
    if mc:
        mc_payload = monte_carlo(
            pnls, n_paths=n_paths, seed=seed, method=mc_method, equity_0=equity_0
        )
    return {
        "mode": "paper",
        "liveDisabled": True,
        "window": window,
        "n_trades": n,
        "trade_count": n,
        "wins": wins,
        "losses": losses,
        "win_rate": win_rate,
        "expectancy": expectancy,
        "max_drawdown_pct": _max_drawdown_pct(equity),
        "sample_ok": sample_ok,
        "mc": bool(mc),
        "monte_carlo": mc_payload,
        "equity": equity,
        "journal": [t.as_dict() for t in trades[-JOURNAL_N:]],
        "equity_0": equity_0,
        "disclaimer": DISCLAIMER,
        "empty": n == 0,
    }


PaperLedger = PaperTradeJournal
_ledger: Optional[PaperTradeJournal] = None
_PERSIST_LOCK = threading.Lock()


def _journal_path() -> Path:
    raw = (os.getenv("PAPER_JOURNAL_STORE") or "").strip()
    if raw:
        return Path(raw)
    return Path(__file__).resolve().parents[2] / "data" / "paper_journal.json"


def _write_journal(path: Path, journal: PaperTradeJournal) -> None:
    payload = {
        "equity_0": journal.equity_0,
        "fills": list(journal.fills),
        "lots": {sym: [lot.as_dict() for lot in lots] for sym, lots in journal.lots.items()},
        "closed": [t.as_dict() for t in journal.closed],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with _PERSIST_LOCK:
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        tmp.replace(path)


def _load_journal(path: Path) -> Optional[PaperTradeJournal]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError:
        return None
    try:
        raw = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(raw, dict):
        return None
    j = PaperTradeJournal(equity_0=float(raw.get("equity_0") or EQUITY_0))
    fills = raw.get("fills") or []
    if isinstance(fills, list):
        j.fills = [f for f in fills if isinstance(f, dict)]
    lots = raw.get("lots") or {}
    if isinstance(lots, dict):
        for sym, rows in lots.items():
            if not isinstance(rows, list):
                continue
            j.lots[str(sym)] = [OpenLot.from_dict(r) for r in rows if isinstance(r, dict)]
    closed = raw.get("closed") or []
    if isinstance(closed, list):
        j.closed = [RoundTrip.from_dict(r) for r in closed if isinstance(r, dict)]
    return j


def repair_closed_entry_impacts(journal: PaperTradeJournal) -> int:
    """Write gross/fee/net onto closes that only have a legacy gross, or copy
    an impact already stored on the opening fill. Does not invent a number.
    """
    changed = 0
    pool: dict[str, list[tuple[int, tuple[Optional[float], Optional[float], Optional[float]]]]] = {}
    for raw in journal.fills:
        if not isinstance(raw, dict):
            continue
        qty = _opt_float(raw.get("qty"))
        side = str(raw.get("side") or raw.get("signal_side") or "").lower()
        if side in {"sell", "short"}:
            continue
        if qty is not None and qty < 0:
            continue
        triple = impact_triple_from_mapping(raw)
        if triple[0] is None:
            continue
        sym = str(raw.get("symbol") or "")
        pool.setdefault(sym, []).append((int(raw.get("ts") or 0), triple))

    for trade in journal.closed:
        gross = trade.entry_estimated_impact_gross_bps
        if gross is None:
            gross = trade.entry_estimated_impact_bps
        if gross is None:
            cands = pool.get(trade.symbol) or []
            if not cands:
                continue
            best_i = min(range(len(cands)), key=lambda i: abs(cands[i][0] - int(trade.entry_ts)))
            _ts, triple = cands.pop(best_i)
            gross, fee, net = triple
        else:
            _g, fee, net = split_impact_gross_fee_net(
                gross,
                protocol_fee_bps=trade.entry_protocol_fee_bps,
                net_bps=trade.entry_estimated_impact_net_bps,
            )
        if gross is None:
            continue
        before = (
            trade.entry_estimated_impact_bps,
            trade.entry_estimated_impact_gross_bps,
            trade.entry_estimated_impact_net_bps,
            trade.entry_protocol_fee_bps,
        )
        trade.entry_estimated_impact_bps = float(gross)
        trade.entry_estimated_impact_gross_bps = float(gross)
        trade.entry_protocol_fee_bps = float(fee) if fee is not None else None
        trade.entry_estimated_impact_net_bps = float(net) if net is not None else None
        after = (
            trade.entry_estimated_impact_bps,
            trade.entry_estimated_impact_gross_bps,
            trade.entry_estimated_impact_net_bps,
            trade.entry_protocol_fee_bps,
        )
        if after != before:
            changed += 1
    return changed


def get_paper_ledger() -> PaperTradeJournal:
    global _ledger
    if _ledger is None:
        path = _journal_path()
        loaded = _load_journal(path)
        _ledger = loaded if loaded is not None else PaperTradeJournal()
        _ledger.persist_path = path
        if repair_closed_entry_impacts(_ledger):
            _ledger._maybe_persist()
    return _ledger


def get_paper_journal() -> PaperTradeJournal:
    return get_paper_ledger()


def reset_paper_ledger(*, wipe_store: bool = True) -> None:
    global _ledger
    path = _journal_path()
    if wipe_store:
        for p in (path, path.with_suffix(path.suffix + ".tmp")):
            try:
                p.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass
    _ledger = None


def reset_paper_journal(*, wipe_store: bool = True) -> None:
    reset_paper_ledger(wipe_store=wipe_store)


def build_performance(
    *,
    window: str = "session",
    from_ts: Optional[int] = None,
    to_ts: Optional[int] = None,
    n_paths: int = DEFAULT_MC_PATHS,
    seed: int = DEFAULT_MC_SEED,
    mc: bool = False,
    mc_method: str = "shuffle",
) -> dict[str, Any]:
    from app.strategies.pump_paper_v1 import get_engine

    engine = get_engine()
    n_window: Optional[int] = None
    window_label: str | int = "session"
    raw = str(window).strip().lower()
    if raw not in {"", "session", "all"}:
        try:
            n_window = max(1, int(raw))
            window_label = n_window
        except ValueError:
            window_label = "session"
    journal = get_paper_journal()
    trades = journal.closed_in_window(window=n_window, from_ts=from_ts, to_ts=to_ts)
    data = summarize(
        trades,
        n_paths=n_paths,
        seed=seed,
        window=window_label,
        mc=mc,
        mc_method=mc_method,
        equity_0=journal.equity_0,
    )
    data["open_lots"] = sum(len(v) for v in journal.lots.values())
    data["fill_count"] = len(journal.fills)
    data["auto_paper_orders"] = engine.params.auto_paper_orders
    data["strategy_autopaper"] = engine.params.auto_paper_orders
    data["strategyId"] = "pump-paper-v1"
    return data
