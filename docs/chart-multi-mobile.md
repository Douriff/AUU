# Chart: multi-chart layout + mobile polish (KLineChart step 4)

Front-end only. No API, trading or exchange code is touched; nothing to migrate.

## Multi-chart (1 / 2 / 4)
- Switcher in the chart header (`data-testid=layout-1|2|4`). Cell 0 is the page's main chart (symbol / timeframe from the URL as before); cells 1-3 have their own symbol + timeframe pickers (native `<select>`, mobile friendly).
- Layout (`n`, synced-crosshair flag, symbol/timeframe of cells 1-3) is persisted in `localStorage` key `auu.kc.layout.v1` (per browser profile). Server-side persistence would need a new endpoint, deliberately not done; helpers in `components/chart/layout.ts` are pure and unit-tested (`npm run test:layout`). Unknown symbols (delisted, hostile values) fall back to defaults.
- Synced crosshair (default on, toggle `⌖`): hovering one chart draws a dashed vertical line at the same bar time in the others (works across timeframes; hidden when the time is outside the other chart's data). Implemented with a tiny pub/sub (`crosshairBus.ts`) + `convertToPixel`, no KLineChart internals.
- Cells are "compact": candles + MA only (session-only indicator set, the main chart's saved indicators are untouched), one-row footer. Drawings / price alerts / AUU signal toggles stay on the single chart view (a cell with the same symbol as another shares that symbol's drawings).
- Desktop multi mode hides the order book column so the grid gets the width; phones stack 2 charts, show 4 as 2x2.

## Mobile
- `viewport-fit=cover` + `--sa-*` safe-area variables: tab bar, bottom sheets, fullscreen chart and landscape side padding respect notch / home indicator.
- Fullscreen chart button (all widths, `Esc` exits, page scroll locked).
- Landscape phones (short + wide) get the phone shell (tab bar, no overflowing desktop nav) and a vertical tool rail; 761-1100px widths no longer overflow horizontally (nav links scroll, short mode badge).
- Zoom -/+ buttons on touch devices (pinch still works), 36-44px tap targets, text selection / callout disabled on the canvas.
