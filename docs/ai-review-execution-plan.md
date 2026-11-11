# Later execution channel

This phase does not send orders. The draft on `/lab/ai-review` is a session preview: it is not saved, it stays disarmed, and it does not authorize live trading.

A later phase can connect real paper or live orders only after a separate arming step. That step is not this development change and not a model response.

## What the user can choose

The user sets paper or live intent, per-order confirmation or automatic action inside the limits, the coin list, spot or perpetual, long-only or long/short, capital and per-order caps, leverage, a hard stop, daily loss and drawdown caps, concurrent positions, slippage, and how old a quote may be. Empty fields stay empty. There is no hidden live balance or leverage.

## How a future order would be sent

- The model does not receive tools and does not call the exchange.
- An order, if one is ever armed, goes through the existing exchange layer already in this repository.
- Risk checks are deterministic code: symbol, market, direction, size, leverage, hard stop, daily loss, drawdown, open-position count, slippage, and quote age. A failed check does not send.
- Each order has an idempotent id. Repeating the same id does not create a second order. A rate limit applies.
- The channel requires a balance read, a fill receipt, and a reconciliation against that receipt before the position is treated as open.
- A hard stop has to be available at the venue or the order is refused.
- Changing a risk limit requires arming again. Reload and restart do not arm.
- A network error, a stale quote, or an expired authorization fails closed: no order.
- One user's draft cannot read or move another user's account.
- A kill switch blocks new orders until a person arms the channel again.
- The audit record is the request, the risk decision, the idempotent id, and the venue receipt. It does not include exchange secrets.
- The channel does not withdraw funds and does not run a shell.
- Nothing in the channel promises a profit.

## What is still not done

- No production relay call from this environment. The adapter can call `https://ai.orbitlink.me/v1/chat/completions` only when `AUU_AI_RELAY`, a model name, `AUU_AI_RELAY_API_KEY`, and a positive budget are set on the server. This workspace does not have that key and does not send the request.
- No persistent execution store, no arming endpoint, and no call into the existing order routes.
- The relay model list in an operator screenshot is not treated as a verified catalog.
