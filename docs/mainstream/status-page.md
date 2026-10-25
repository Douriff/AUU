# Public status page (P1-6)

`/admin/status` (web, administrators only) and `GET /api/v1/status` (admin-only when accounts
are on; health stays public for monitors). Signed-out visitors and regular accounts do not see
the page or a login-page link to it.

Shows only coarse fields: API up, market data fresh (count of stale series), exchanges blocked
(count, no names or error text), last rebalance time and day / overdue flag, live trading locked,
and a 30-day availability record. No positions, NAV, prices, users, versions, hosts or paths.

## Availability record

- The API writes one row per minute (`uptime.sqlite` in the data dir; `AUU_UPTIME=off` disables).
  A minute is *healthy* when the same checks auu-guard asserts all pass: live locked, market data
  fresh, strategy rebalance not overdue. Several probes in one minute: the worst wins.
- Minutes with no row count as down (API process stopped or host down).
- Measured from the first recorded minute (or the 30-day window start); the current minute is
  excluded because it may not be written yet. Rows older than 90 days are pruned.
- Outages listed: gaps (down) and runs of unhealthy minutes (degraded) of at least 5 minutes,
  newest first, at most 10.
- Limits: this is self-reported by the API process. It does not see network or TLS problems in
  front of the API; an external probe would be needed for that.
