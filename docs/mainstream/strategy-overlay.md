# Strategy signals on the chart (P1-5)

Read-only data for a daily-chart overlay; nothing here places orders or changes a decision.
This PR ships the API (+ TS type and provider method). The chart wiring (markers via
lightweight-charts `setMarkers`, look-back lines on a separate price scale, a "策略信号" toggle on
the daily chart) is left to the UI redesign so it lands in the new chart component.

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
