"""LiveBroker stub — same order lifecycle as PaperBroker, venue=live, blocked by the gate.

This PR does not submit chain transactions. When the hard gate is closed, submit
rejects with LIVE_DISABLED / NO_KEYPAIR. When the checklist
passes, submit still refuses with LIVE_STUB. Official `@pump-fun/pump-sdk`
buy/sell wiring is documented in docs/adapters/pumpfun-live-local-signer-v0.md
and is not called here.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from app.live.gate import REASON_LIMITS_MISSING, evaluate
from app.models.contracts import Fill, OrderIntent, RejectOut, RiskOut, SignalOut, SizeIn, StrategyContext
from app.risk import get_risk_gate

log = logging.getLogger("auu.live.broker")

VENUE = "live"

# Future (not this PR): PumpSdk.buyInstructions / sellInstructions from the
# official MIT pump-sdk, signed by LocalSigner, only after evaluate().armed
# and LIVE_SEND_WIRED. See docs/adapters/pumpfun-live-local-signer-v0.md.


@dataclass
class LiveBroker:
    venue: str = VENUE
    open_orders: list[OrderIntent] = field(default_factory=list)
    last_reject: Optional[RejectOut] = None

    def submit(self, ctx: StrategyContext, intent: OrderIntent) -> list[Fill]:
        """Reject unless armed; even when armed, do not submit a chain transaction."""
        self.last_reject = None
        status = evaluate()
        if not status.armed:
            notes = "live gate closed: " + ",".join(status.reasons)
            log.info("live submit blocked (%s)", ",".join(status.reasons))
            self._reject(list(status.reasons), notes)
            return []
        log.info("live submit armed but stubbed; no chain submit in this PR")
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
    if not status.limits.complete() or REASON_LIMITS_MISSING in status.reasons:
        risk = RiskOut(
            allow=False,
            clipped_size=None,
            tags=["LIMITS_MISSING"],
            notes="live limits missing or zero: " + ",".join(status.limits_missing),
        )
    else:
        risk = gate.check_live(ctx, signal, size, live_limits=status.limits.as_dict())
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
        gate.on_reject(ctx, risk.tags)
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
        hub = get_hub()
        await hub.publish(
            {
                "type": "reject",
                "payload": {
                    "ts": ctx.ts,
                    "symbol": ctx.symbol,
                    "tags": list(status.reasons),
                    "notes": notes,
                    "venue": VENUE,
                },
            }
        )
        return _reject_payload(ctx, list(status.reasons), notes)
    if not risk.allow:
        return _reject_payload(ctx, ["RISK_DENIED"], "risk.allow must be true")

    broker = get_live_broker()
    fills = broker.submit(ctx, intent)
    hub = get_hub()
    if not fills:
        reject = broker.last_reject
        tags = reject.tags if reject else ["LIVE_STUB"]
        notes = reject.notes if reject else "no fill"
        get_risk_gate().on_reject(ctx, tags)
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

    # Unreachable in this PR: LiveBroker never returns fills. Kept so the
    # lifecycle matches PaperBroker if a later PR wires official pump-sdk.
    fill_payloads: list[dict[str, Any]] = []
    trading_state: Optional[str] = None
    gate = get_risk_gate()
    for f in fills:
        dumped = f.model_dump()
        dumped["symbol"] = ctx.symbol
        dumped["venue"] = VENUE
        fill_payloads.append(dumped)
        await hub.publish({"type": "fill", "payload": dumped})
        if auto_post_fill:
            result = gate.post_fill(ctx, f)
            trading_state = result["trading_state"]
    data: dict[str, Any] = {"fills": fill_payloads, "venue": VENUE}
    if trading_state:
        data["trading_state"] = trading_state
    return data
