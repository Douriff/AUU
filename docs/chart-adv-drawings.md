# Chart advanced drawing tools (KLineChart step 5)

Frontend: `apps/web/src/components/chart/kline/advOverlays.ts` (registerOverlay), geometry in `advMath.ts` (unit tests: `npm run test:adv`).

| Tool id | Points | What |
|---|---|---|
| `auuFibExt` | 3 (A,B,C) | Fibonacci extension 0/0.618/1/1.272/1.618/2/2.618 from C, projected by (B-A) |
| `auuPitchfork` | 3 | Andrews pitchfork (median line + two parallels) |
| `auuFibChannel` | 3 | Fibonacci channel (parallel rays at fib ratios) |
| `auuMeasure` | 2 | Δprice, Δ%, bars, time span in a label box |
| `auuPriceRange` | 2 | Price band with Δ label |
| `auuDateRange` | 2 | Time band with bars/span label |

Persistence reuses the existing pipeline (localStorage + `/api/v1/chart/drawings`); only `name` + `points` are stored.

**Deploy note:** `apps/api/app/chart_drawings.py` `ALLOWED_TOOLS` gained the six ids (additive, stored documents stay valid).
The API process (Tokyo and HK standby) must be restarted. Deploy backend first; if the frontend is live first,
PUTs with new tools get HTTP 400 and the client keeps them local-only (no retry loop) until the API is restarted.
