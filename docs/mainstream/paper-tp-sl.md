# Manual paper take-profit / stop-loss (OCO)

Manual paper trading only (`/api/v1/mainstream/paper/*`, the "止盈止损" tab of the trade panel).
The automated strategy (`trend_tsmom_v1` runner) does not place or read these orders, and the
live lock is unchanged.

## Order types

`POST /api/v1/mainstream/paper/orders`

| type | fields | meaning |
|---|---|---|
| `take_profit` | `trigger_price`, `qty` or `notional` | sell when a 1m bar's high reaches the trigger (trigger must be above the current price) |
| `stop_loss` | `trigger_price`, `qty` or `notional` | sell when a 1m bar's low reaches the trigger (trigger must be below the current price) |
| `oco` | `take_profit_price`, `stop_loss_price`, `qty` or `notional` | both legs; when one fills the other is cancelled (`OCO_CANCEL`) |

- Sell only: protects an existing spot long. The quantity must be available (not reserved by other
  sell orders); an OCO pair reserves its quantity once.
- Existing limits apply: per-order min/max notional (checked at each trigger price), max open orders
  (each leg counts), orders per minute, ±50% price band. Per-coin position cap is unaffected (sells).
- Idempotent: leg client ids are `<client_order_id>-tp` / `-sl`; resubmitting returns the same legs.
- Cancelling either OCO leg cancels the pair.

## Fill model

- Triggers are checked on the same lazy pass as limit orders (each account snapshot), from the 1m
  candles the market cache already keeps (`store.candles`, throttled tail refresh). One local read
  per (symbol, start) per pass; no new exchange requests.
- Bars from the minute after placement are checked; a per-order checkpoint (`checked_ts`) moves
  forward so long-resting orders keep being checked. The newest (possibly still forming) bar is
  checked again next time.
- Fill = market sell: take-profit at max(trigger, bar open), stop-loss at min(trigger, bar open)
  (gap-through fills at the open), minus the CostModel slippage, taker fee.
- One bar touching both legs: the stop-loss is assumed first (conservative).
- If the position is gone at trigger time the leg is cancelled (`INSUFFICIENT_POSITION`).
- Reasons on filled legs: `TP_TRIGGERED` / `SL_TRIGGERED`.

Schema: `orders` gains `trigger_px`, `oco_group`, `checked_ts` (added in place; old rows NULL).
