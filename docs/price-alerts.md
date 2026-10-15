# Price alerts (chart, step 3)

User-set price alerts on the mainstream chart (BTC/ETH/SOL ... every coin the market-data service tracks).
Chart UI data only: nothing here touches ledgers, strategy, points, exchange or trading code.

## API (login required; `/api/v1/chart/alerts`)

| Method | Path | Body / query | Notes |
|---|---|---|---|
| GET | `/api/v1/chart/alerts` | `?status=active\|triggered\|cancelled&symbol=BTC` | `{alerts[], caps{active,maxActive,triggered}, unread, emailOn, now}` |
| POST | `/api/v1/chart/alerts` | `{symbol, price, direction: above\|below\|cross, note?, venue?}` | 409 `ALERT_LIMIT` at 20 active, 409 `ALERT_DUPLICATE`, 400 `ALREADY_REACHED` (an `above` alert at/below the last price, or `below` at/above it) |
| DELETE | `/api/v1/chart/alerts/{id}` | | owner only (404 otherwise) |
| DELETE | `/api/v1/chart/alerts?scope=history` | | clears triggered / cancelled rows |
| GET | `/api/v1/chart/alerts/notifications` | `?unread=1&limit=50` | in-site notifications (badge) |
| POST | `/api/v1/chart/alerts/notifications/read` | `{ids?: int[]}` | no ids = all read |

Limits: 20 active alerts per user, 100 history rows, 200 notifications kept, note <= 80 chars, price in (0, 1e9],
20 writes/min and 240 reads/min per user (429 `RATE_LIMITED`). Server messages are Chinese (the zh-CN UI shows them
as is); other languages use `apiErr.*` keys.

## Table (`price_alerts.sqlite`, own file, 0600)

`price_alerts(id, user_id, symbol, venue, price, direction, created_at, triggered_at, note, status, side, trigger_price)`
plus `price_alert_events` (in-site notification + mail outbox: `read_at`, `email_status`, `email_attempts`, ...).
Created on first use. `venue` = exchange the price came from when the alert was set (informational; the checker
uses the market-data service's active exchange).

## Checker (primary only)

`app.price_alerts.run_loop` is started in the API lifespan only when `price_alerts_enabled()`:
not `AUU_ROLE=standby` (so **HK never checks or mails**) and `AUU_PRICE_ALERTS` not off (default on).
Every 15 s, only if at least one alert is active, it reads `MainstreamService.markets()` (cached ticker prices, the
same 30 s ticker refresh the market list already uses) and ignores prices older than 10 minutes.

Fire-once: each alert stores `side` (price below target = -1, at/above = +1). It fires when the side flips in the
alert's direction (`above`: up-cross, `below`: down-cross, `cross`: either). The first price seen only records the
side. The row moves `active -> triggered` with a compare-and-set `UPDATE ... WHERE status='active'`, so restarts,
two checkers or a price wobbling around the target cannot fire it twice. Limit: the ticker is polled every 30 s, so a
wick that crosses and returns between two polls is not seen.

## Notifications

* In-site: unread count badge on the chart's bell button (polled every 30 s while the tab is visible), a toast when a
  new alert fires, a "Recently triggered" list with a "New" tag.
* Email: to the user's registered address via the existing AUUTRADE SMTP channel (`auth.email_codes._smtp_send`),
  localized with the user's interface language (11 languages), AUUTRADE signature from `auth.email_i18n`, plain text
  + HTML. Retry with backoff (2, 4, 8 min, then every 10 min) for 24 h; at most 30 alert mails per user per hour
  (beyond that in-site only). Users without an email (or SMTP not configured) get in-site only.
  The operator alert mailer (`app.alerts`, `AUU_ALERT_TO`) is not used and not changed.

## Standby

`/api/v1/chart/` writes already answer 503 on a standby (role middleware); the alert routes also answer 503 for reads,
and the checker is off. `price_alerts` is listed in `role.STANDBY_OFF`.

## Deploy notes

API restart needed. Add `price_alerts.sqlite` to the Tokyo backup list. No env change needed (`AUU_PRICE_ALERTS`
defaults to on; set `off` to disable the checker).
