# PumpPortal WS 400 / 403 (discovery)

Paper-only. Does **not** enable live. See implementation: `docs/adapters/pumpportal-discovery-v0.md`.

PumpPortal Data API (https://pumpportal.fun/data-api/real-time/):

- URI: `wss://pumpportal.fun/api/data?api-key=...`
- Subscribe: `subscribeNewToken` (AUU uses this only)
- **One WebSocket at a time.** Opening many connections can time the client out; bans expire about hourly.

## Status codes AUU treats as `portal_auth_rejected`

| Code | Meaning | What we saw in prod |
|------|---------|---------------------|
| **400** | Bad request / malformed query | Two 103-char keys concatenated with a space inside a quoted `PUMPFUN_PORTAL_API_KEY` |
| **403** | Auth / ban | Each token alone (invalid, expired, or banned) **or** a temporary IP ban from a retry storm |

Do not hammer reconnect every few seconds after 400/403. Back off (cap ~5 minutes) and/or fall back to `PUMPFUN_DISCOVERY=logs` when `SOLANA_RPC_URL` is set.
