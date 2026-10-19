/** Reconnect delay for the market-data WebSocket (docs/realtime-ws-plan.md step 3).
 *
 *  ``base * 2^(attempt-1)`` capped at ``cap``, then multiplied by a random factor in [0.5, 1) ("equal jitter"):
 *  after a server / Cloudflare restart every open page sees the close at the same instant, and the jitter spreads
 *  their reconnects over half the window instead of a synchronized storm. */
export const BACKOFF_BASE_MS = 1_000;
export const BACKOFF_CAP_MS = 30_000;

export function backoffDelay(attempt: number, rand: () => number = Math.random, base = BACKOFF_BASE_MS, cap = BACKOFF_CAP_MS): number {
  const n = Math.max(1, Math.floor(attempt));
  const raw = Math.min(cap, base * 2 ** (n - 1));
  return Math.round(raw * (0.5 + 0.5 * Math.min(1, Math.max(0, rand()))));
}
