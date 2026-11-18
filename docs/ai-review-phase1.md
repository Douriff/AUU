# Strategy review lab (phase 1)

Isolated, read-only admin page for comparing registered strategies, reading a stored signal, and listing decision records. It does not place orders, change positions, start or stop a strategy, open a wallet, charge a fee, or turn on the site WebSocket.

## URLs

- Page: `/lab/ai-review` (lazy route, not linked from the existing navigation)
- API prefix: `/api/v1/ai-review` (GET only)

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/v1/ai-review/plaza` | Board, comparison, signal preview, featured rule note, decision lanes |
| GET | `/api/v1/ai-review/plaza.csv` | Same board as CSV. Cells that start with `= + - @` or a tab are prefixed so a spreadsheet does not treat them as formulas. |
| GET | `/api/v1/ai-review/compare?ids=` | Same-window comparison for the named ids |
| GET | `/api/v1/ai-review/strategies/{id}` | One registered identity |
| GET | `/api/v1/ai-review/signals` | Stored signal rows |
| GET | `/api/v1/ai-review/signals/explain` | Rule replay for one bar |
| GET | `/api/v1/ai-review/decisions` | Stored history, split by book |

POST, PUT, PATCH, and DELETE return 401 or 403 before a 405. There is no write.

## Who can open it

The account system must be on (`AUU_AUTH=on`) and the session cookie must belong to a user whose `is_admin` flag is true. Anonymous requests are 401. A signed-in non-admin is 403 and the body has no strategy payload. With accounts off, the page stays closed even if a cookie, `X-Admin`, or `localStorage` flag is present. The existing login gate for the rest of the API is unchanged.

## Where numbers come from

The lab reads local SQLite files only when they already exist, in read-only mode. It does not create a database, table, or row.

| Strategy | Identity source | Performance source | If the file or table is absent |
| --- | --- | --- | --- |
| `auu_signal_v0`, `cand_A`, `cand_B`, `cand_A_ls`, `cand_B_ls` | Frozen code in `app/strategies` | `auu_signal_shadow.sqlite` tables `cand_nav`, `cand_trades`, `cand_bars` / `bars` | Status `not_connected`. Return, drawdown, cost, and sample counts are null. |
| `trend_tsmom_v1` | `TrendTSMOM` code default | `mainstream_strategy.sqlite` runs whose strategy name matches, plus `meta.start_nav` | Not connected. The process default starting NAV is not invented. |
| `shadow_s3` | Frozen `RULE` | Trade rows only. There is no portfolio NAV, so return and drawdown stay null. | Not connected. |
| `shadow_h2` | Frozen params | `shadow_h2.sqlite` `days`, checked against the frozen accounting unit | Not connected. |
| Backtest | none in this repo | none | Always `not_connected`. Research tables in `docs/signal-candidates.md` are not copied onto the cards. |

A zero is shown only when the stored series itself is zero. Signal-row counts are labeled separately and are not trade samples. NAV and return must chain; if they disagree, both headline figures are withheld. Costs for the signal candidates are shown only when every closed trade matches the registered 7 bps per side identity. Open trades keep unrealized results null. Different timestamp lists are refused as a comparison. Fewer than 30 NAV points or closed trades, or a daily series older than one bar plus 36 hours, is flagged.

Late entry and early exit are not scored. This repo has no pre-registered breakout detector that this page can apply, so the timing block stays `not_measured`.

## Rule notes and the relay

The explanation replays the frozen rule from the stored score and the previous stored bar. A gap is more than 1.5 bars of that series, including hourly bars. `auu_signal_v0` has no parameter hash in the bar table, so a matching replay is `params_hash_not_stored` and is not a verified binding. A candidate whose stored `params_hash` matches the frozen registration can be verified. There is no invented stop.

Reads are per strategy. If a strategy has more rows than the cap, its return, drawdown, and cost stay null with reason `truncated`. Paper fills have no strategy column; a fill is attributed only by joining the single `runs` row on that day. A fill with no matching run stays unassigned. `GET /signals?limit=` pages the stored bars and reports `truncated` when more rows exist. It does not reuse the 12-row board preview.

The relay speaks `POST https://ai.orbitlink.me/v1/chat/completions` with a bearer token. Other hosts, private addresses, and redirects are refused. The key is the server environment variable `AUU_AI_RELAY_API_KEY` and is never returned. A call needs `AUU_AI_RELAY=on`, `AUU_AI_RELAY_MODEL`, and either a budget from 1 to 1000 or the exact word `unlimited`. The default sends nothing. Opening the page and `GET` explanation do not call the relay. An administrator sends one note with `POST /api/v1/ai-review/interpret` from the signal or decision button. The server builds the facts from the stored row. The browser cannot choose the model or send a prompt. `gpt-4o-mini` is recorded as one checked candidate and is not the model that is sent unless `AUU_AI_RELAY_MODEL` is that name.

The daily ceiling is `AUU_AI_RELAY_BUDGET`. Missing or `0` sends nothing. A whole number from 1 to 1000 is that many upstream requests per UTC day. The exact word `unlimited` removes only that count. `1001`, `none`, `inf`, and any other text are rejected and send nothing; they are not stored as 1000. `unlimited` is not a dollar cap. The count is stored in `ai_relay_dispatch.sqlite` under the data directory. That file is not a strategy or account ledger. The attempt is reserved in that file before the socket is opened, and it is kept if the relay times out, returns bad JSON, returns more than 2000 characters, or returns a note that cannot be checked. Nothing is refunded and nothing is retried. A private address, a closed budget, a busy concurrency slot, or the same server-fact hash does not open a socket and does not add an attempt. The hash is computed from the stored facts, not from the browser. The same hash is not dispatched again. There is no token price in this service, so the counter is not a dollar cost and `budget_guaranteed` stays false. A process restart does not clear the day's count. Concurrency defaults to 1 (`AUU_AI_RELAY_CONCURRENCY`, at most 4). The timeout and `max_tokens` stay on the server.

The model is told to return one JSON object whose only keys are `summary` and `citations`, to copy numbers exactly, and not to turn a timestamp into a calendar date. A JSON object that is only the facts, a calendar date, or a citation whose value is not the stored value is rejected. The reason is a short code. The model text is not stored. `max_tokens` stays 400; a note that copies one stored word and one citation is far smaller than that. That rejection still counts when the request was dispatched. A fact number and its percent form are allowed. The note is not a return and not an order. `invoked` is true once today's attempt count is above zero. It does not by itself verify an operator's model list.

The execution draft validates limits and always returns disarmed and not connected. It is not saved. See `docs/ai-review-execution-plan.md`.

`AUU_AI_REVIEW_FIXTURE=on` loads `app/ai_review/fixtures/dev_sample.json` and does not read ledgers. The page is marked as a sample. The flag defaults off. An invalid fixture does not fall back to another source.

## What was registered in the existing app

- `apps/api/app/main.py` includes the new router.
- `apps/web/src/app/router.tsx` lazy-loads `/lab/ai-review`.
- The eleven locale files gained an `aiReview` object. Keys that already existed on `main` are unchanged.

No navigation item, lifespan task, database migration, auth-gate rule, or existing route behavior was changed.

## Deploy and roll back

This phase is not deployed by this change. To roll back, revert the commit. There is no schema migration and no new process. Unset `AUU_AI_REVIEW_FIXTURE` if it was turned on for a screenshot. Leave `AUU_AI_REVIEW_MODEL` unset.

The page refreshes only while it is mounted and the document is visible, at most one request at a time, every 60 seconds. It does not open the market WebSocket.
