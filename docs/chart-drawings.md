# Chart drawings: server persistence (KLineChart step 2)

Drawings (trendline, ray, horizontal/vertical line, fib, rectangle, parallel channel, text) are kept per
account and symbol. The browser keeps a local copy (instant, offline-safe); the server copy makes them follow
the account across devices.

## API (login required, JSON envelope `{ok, data}`)
| Call | Purpose |
|---|---|
| `GET /api/v1/chart/drawings?symbol=BTC[&tf=1h]` | `{symbol, tf, drawings[], updatedAt}` (`updatedAt` 0 = nothing stored) |
| `PUT /api/v1/chart/drawings` `{symbol, tf?, drawings[], baseUpdatedAt?}` | replace the document; an empty list deletes it; `409 CONFLICT` + `error.current` when `baseUpdatedAt` is stale |
| `GET /api/v1/chart/drawings/index` | which charts have drawings (symbol, tf, count, updatedAt) |

`tf` is empty for drawings shared by all timeframes (what the web app uses); the API also accepts 1m..1d.

## Limits and validation (`app/chart_drawings.py`)
- tools whitelisted: `segment rayLine horizontalStraightLine verticalStraightLine fibonacciLine auuRect parallelStraightLine auuText` + step 5: `auuFibExt auuPitchfork auuFibChannel auuMeasure auuPriceRange auuDateRange`
- ≤ 200 drawings per chart, 1..5 points each, finite numbers, timestamps 2000..2100, text ≤ 200 chars,
  `styles` ≤ 2 KB plain JSON, document ≤ 60 KB (nginx `client_max_body_size` is 64k), ≤ 300 charts per account
- rate limit per account: 30 PUT/min, 240 GET/min (429 `RATE_LIMITED`); the global shield limits still apply
- standby node: `/api/v1/chart/` writes answer 503 STANDBY (the DB is not synced to the standby; a write there would fork it);
  the web app then simply stays on the local copy

## Storage
`chart_drawings.sqlite` (own file next to the other DBs, mode 0600), table
`chart_drawings(user_id, symbol, tf, json, updated_at, PRIMARY KEY(user_id, symbol, tf))`.
Pure UI data: no ledger, strategy, points or exchange code reads or writes it. Created lazily on first use.
Not in the HK ledger sync list and not in the deploy-script backup list (add it there if you want it backed up).

## Web sync (`src/components/chart/kline/drawingsSync.ts`)
Per symbol: local drawings + `{base, dirty}`. Legacy step-1 local drawings (no sync meta) are migrated by merging
with the server copy; a clean local copy follows the server (incl. deletes made elsewhere); dirty edits are pushed
(debounced, one request at a time); if both sides changed they are merged (union, nothing lost). Failures keep the
edits locally and retry (30 s timer, `online` event, next edit). Status dot in the toolbar: Local only / Saving / Synced / Error.
Unit tests: `npm run test:sync`.
