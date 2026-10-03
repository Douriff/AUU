# Strategy signals on the chart (P1-5)

Daily chart → "策略信号" toggle (mainstream page). Read-only; nothing here places orders or
changes a decision.

- Markers: every M3 paper-runner rebalance fill for the coin (ledger `fills`), on the daily bar
  whose close the rebalance used. ▲ buy / ▼ sell, text = target weight after the trade.
- Lines (bottom band, % scale): the TSMOM look-back returns close_t / close_{t-L} - 1 for
  L = 20 / 60 / 120 — the inputs the strategy signs and averages. Dashed line = 0.
- Legend (follows the crosshair): the three returns, signal (mean of signs, long-only clipped at 0)
  and target weight for that day.
- Source: `GET /api/v1/mainstream/strategy/overlay?symbol=&days=` (login required). It runs the
  strategy's own `targets()` on the same store panel the runner reads (`runner.panel_fn(due_day)`),
  so the target shown for a fill day equals the fill's `w_to` (tested). Cached 10 minutes per
  (coin, day).
- Recorded vs recomputed: for days the runner ran, the legend shows the target it recorded
  (`runs.targets`). If the data under that day changed afterwards (late backfill, a coin added to
  the store, which changes N in the 1/N sizing), the recomputed target differs; the legend shows
  both and the count of such days (`revised`). The ledger is never rewritten.
- Days without a marker: the target moved less than the rebalance band, so no trade.
