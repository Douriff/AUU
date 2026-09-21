"""LiveBroker stub — same order lifecycle as PaperBroker, venue=live, blocked by the gate.

This PR does not submit chain transactions. When the hard gate is closed, submit
rejects with LIVE_DISABLED / NO_KEYPAIR. When the checklist
passes, intent is built from official pump-sdk method names only, then the
send gate (outside liveDisabled) refuses. Official `@pump-fun/pump-sdk`
buyInstructions / sellInstructions are documented, not called.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from app.live.gate import REASON_LIVE_DISABLED, evaluate
from app.live.intent import build_intent_for_order
from app.live.ledger import get_live_ledger
from app.live.send import refuse_send, send_allowed
from app.models.contracts import Fill, OrderIntent, RejectOut, RiskOut, SignalOut, SizeIn, StrategyContext
from app.risk import get_risk_gate

log = logging.getLogger("auu.live.broker")

VENUE = "live"


@dataclass
class LiveBroker:
    venue: str = VENUE
    open_orders: list[OrderIntent] = field(default_factory=list)
    last_reject: Optional[RejectOut] = None
    last_intent: Optional[dict[str, Any]] = None

    def submit(self, ctx: StrategyContext, intent: OrderIntent) -> list[Fill]:
        """Reject unless armed; even when armed, do not submit a chain transaction."""
        self.last_reject = None
        self.last_intent = None
        status = evaluate()
        if not status.armed:
            notes = "live gate closed: " + ",".join(status.reasons)
            log.info("live submit blocked (%s)", ",".join(status.reasons))
            self._reject(list(status.reasons), notes)
            return []
        mint = ""
        if ctx.meta:
            mint = str(ctx.meta.get("mint") or "")
        self.last_intent = build_intent_for_order(
            side=intent.side,
            mint=mint or ctx.symbol,
            qty_or_notional=float(intent.qty_or_notional),
        )
        if not send_allowed():
            tags, notes = refuse_send()
            log.info("live submit intent built; send gate closed")
            self._reject(tags, notes)
            return []
        log.info("live submit send latch open but stubbed; no chain submit in this PR")
        self._reject(
            ["LIVE_STUB"],
            "live broker is a stub; official pump-sdk buy/sell is documented, not called",
        )
        return []

    def _reject(self, tags: list[str], notes: str = "") -> None:
        self.last_reject = RejectOut(tags=tags, notes=notes)

    def on_tick(self, ctx: StrategyContext) -> list[Fill]:
        # No resting live orders in this scaffold.
        return []


_broker: Optional[LiveBroker] = None


def get_live_broker() -> LiveBroker:
    global _broker
    if _broker is None:
        _broker = LiveBroker()
    return _broker


def reset_live_broker() -> None:
    global _broker
    _broker = None


def _reject_payload(ctx: StrategyContext, tags: list[str], notes: str) -> dict[str, Any]:
    return {
        "fills": [],
        "reject": {"tags": tags, "notes": notes},
        "venue": VENUE,
        "symbol": ctx.symbol,
        "ts": ctx.ts,
    }


async def run_live_pre_order(
    ctx: StrategyContext,
    signal: SignalOut,
    size: SizeIn,
    *,
    strategy_id: str = "live-manual",
) -> RiskOut:
    from app.bus import get_hub

    status = evaluate()
    gate = get_risk_gate()
    risk = gate.check_live(ctx, signal, size, live_limits=status.limits.as_dict())
    if not status.armed and REASON_LIVE_DISABLED not in risk.tags:
        merged = [REASON_LIVE_DISABLED] + [t for t in risk.tags if t != REASON_LIVE_DISABLED]
        extra = [t for t in status.reasons if t not in merged]
        risk = RiskOut(
            allow=False,
            clipped_size=None,
            tags=merged + extra,
            notes=risk.notes or ("live gate closed: " + ",".join(status.reasons)),
        )
    hub = get_hub()
    await hub.publish(
        {
            "type": "risk",
            "payload": {
                "strategyId": strategy_id,
                "symbol": ctx.symbol,
                "t": ctx.ts,
                "risk": risk.model_dump(),
                "venue": VENUE,
            },
        }
    )
    if not risk.allow:
        await hub.publish(
            {
                "type": "reject",
                "payload": {
                    "ts": ctx.ts,
                    "symbol": ctx.symbol,
                    "tags": risk.tags,
                    "notes": risk.notes or "",
                    "venue": VENUE,
                },
            }
        )
    return risk


async def run_live_order(
    ctx: StrategyContext,
    intent: OrderIntent,
    risk: RiskOut,
    *,
    auto_post_fill: bool = True,
) -> dict[str, Any]:
    """Pre-checked live submit. Never fabricates a Fill. Never submits a chain tx."""
    from app.bus import get_hub

    status = evaluate()
    if not status.armed:
        notes = "live gate closed: " + ",".join(status.reasons)
        tags = list(status.reasons)
        if REASON_LIVE_DISABLED not in tags:
            tags = [REASON_LIVE_DISABLED] + tags
        hub = get_hub()
        await hub.publish(
            {
                "type": "reject",
                "payload": {
                    "ts": ctx.ts,
                    "symbol": ctx.symbol,
                    "tags": tags,
                    "notes": notes,
                    "venue": VENUE,
                },
            }
        )
        return _reject_payload(ctx, tags, notes)
    if not risk.allow:
        return _reject_payload(ctx, list(risk.tags) or ["RISK_DENIED"], risk.notes or "risk.allow must be true")

    broker = get_live_broker()
    fills = broker.submit(ctx, intent)
    hub = get_hub()
    if not fills:
        reject = broker.last_reject
        tags = reject.tags if reject else ["LIVE_STUB"]
        notes = reject.notes if reject else "no fill"
        await hub.publish(
            {
                "type": "reject",
                "payload": {
                    "ts": ctx.ts,
                    "symbol": ctx.symbol,
                    "tags": tags,
                    "notes": notes,
                    "venue": VENUE,
                },
            }
        )
        payload = _reject_payload(ctx, tags, notes)
        if broker.last_intent:
            payload["intent"] = broker.last_intent
        return payload

    # Unreachable in this PR: LiveBroker never returns fills. Kept so the
    # lifecycle matches PaperBroker if a later PR wires official pump-sdk.
    # Live fills go to the live ledger only — never PaperTradeJournal.
    fill_payloads: list[dict[str, Any]] = []
    ledger = get_live_ledger()
    mint = None
    if ctx.meta:
        mint = ctx.meta.get("mint")
    for f in fills:
        dumped = f.model_dump()
        dumped["symbol"] = ctx.symbol
        dumped["venue"] = VENUE
        dumped["source"] = "live"
        fill_payloads.append(dumped)
        ledger.record_fill(ctx.symbol, f, mint=str(mint) if mint else None)
        await hub.publish({"type": "fill", "payload": dumped})
    return {"fills": fill_payloads, "venue": VENUE}
