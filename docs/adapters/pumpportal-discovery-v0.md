# PumpPortal discovery adapter (read-only)

Paper-only `new_token` ingest. Mode `PUMPFUN_DISCOVERY=pumpportal|logs|off`.  
Subscribe method stays **`subscribeNewToken`**. URI:

`wss://pumpportal.fun/api/data?api-key=...`

Docs: https://pumpportal.fun/data-api/real-time/  
AUU never sends `subscribeTokenTrade` / `subscribeAccountTrade` (metered, sniper-adjacent). No chain txs. `liveEnabled` stays default **false**.

## API key load

`PUMPFUN_PORTAL_API_KEY` is env-only. The loader:

1. Strips wrapping quotes and whitespace.
2. If the value contains **multiple whitespace-separated tokens**, uses the **first** and logs a warning (**never logs the key**).
3. Never concatenates tokens into the WebSocket URI.

A quoted production `.env` that stored two 103-char keys separated by a space produced a combined query string → **HTTP 400**. Each token tried alone → **HTTP 403**.

## Handshake HTTP status (health `discoveryReason`)

| HTTP | Typical meaning | AUU behavior |
|------|-----------------|--------------|
| **400** | Malformed request — concatenated/quoted/spaced key, bad query | `discoveryReason=portal_auth_rejected`; stop the few-second retry storm |
| **403** | Invalid, expired, or banned API key **or** a temporary **IP ban** | same reason code |
| Retry storm / many sockets | Portal docs: **one WebSocket at a time**; clients that open many connections may be timed out. Bans expire about hourly | Exponential backoff, cap **300s (~5 min)** |

Health also exposes `discovery` (configured env) and `discoveryActive` (runtime; may be `logs` after fallback). The key is never returned.

## Fallback

On Portal **400/403**, if `SOLANA_RPC_URL` is set, discovery **auto-falls back** to `logsSubscribe` (still paper watchlist only). Set `PUMPFUN_DISCOVERY_FALLBACK=off` to keep retrying Portal with backoff instead.

Fix the key (single token, not concatenated) before expecting Portal to recover. Do not open a second Portal socket from another process on the same IP.
