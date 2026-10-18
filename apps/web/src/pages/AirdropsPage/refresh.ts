/** Auto-refresh + freshness helpers for the 空投雷达 page (pure, unit-tested: npm run test:airdrop). */

/** The page re-reads /api/v1/airdrops this often while the tab is visible (the server ingests every ~240 s). */
export const AUTO_REFRESH_MS = 60_000;

export interface AutoRefreshOpts {
  intervalMs: number;
  /** Called on every due tick and immediately when the tab becomes visible again. */
  run: () => void;
  isHidden: () => boolean;
  /** Subscribe to visibility changes; returns the unsubscribe function. */
  onVisibility: (cb: () => void) => () => void;
  setTimer?: (fn: () => void, ms: number) => unknown;
  clearTimer?: (id: unknown) => void;
}

/** Runs ``run`` every ``intervalMs`` while visible; the timer is stopped while hidden and, when the tab
 *  shows again, ``run`` fires at once and the cadence restarts. Returns a disposer. */
export function createAutoRefresh(o: AutoRefreshOpts): () => void {
  const setT = o.setTimer ?? ((fn, ms) => globalThis.setInterval(fn, ms));
  const clearT = o.clearTimer ?? ((id) => globalThis.clearInterval(id as number));
  let timer: unknown = null;
  const stop = () => {
    if (timer !== null) clearT(timer);
    timer = null;
  };
  const start = () => {
    stop();
    timer = setT(() => {
      if (!o.isHidden()) o.run();
    }, o.intervalMs);
  };
  const off = o.onVisibility(() => {
    if (o.isHidden()) {
      stop();
    } else {
      o.run();
      start();
    }
  });
  if (!o.isHidden()) start();
  return () => {
    stop();
    off();
  };
}

/** Whole minutes between ``thenMs`` and ``nowMs`` (never negative). */
export function minutesAgo(thenMs: number, nowMs: number): number {
  return Math.max(0, Math.floor((nowMs - thenMs) / 60_000));
}

/** Server-clock "now" estimated from the ``serverNow`` the API returned at ``fetchedAt`` (client clock). */
export function serverClock(serverNow: number | null | undefined, fetchedAt: number, clientNow: number): number {
  return serverNow && serverNow > 0 ? serverNow + Math.max(0, clientNow - fetchedAt) : clientNow;
}

/** Minutes since the server last ingested anything successfully, or null when unknown / not stale.
 *  "Stale" = older than 3 ingest ticks (min 15 min): a source outage, not normal polling lag. */
export function staleMinutes(lastIngestAt: number | null | undefined, serverNowMs: number, ingestIntervalSec: number | undefined): number | null {
  if (!lastIngestAt) return null;
  const limit = Math.max(15 * 60_000, 3 * (ingestIntervalSec ?? 240) * 1000);
  const age = serverNowMs - lastIngestAt;
  return age > limit ? Math.floor(age / 60_000) : null;
}

export interface FeedLike {
  name: string;
  ok: boolean;
  lastAttemptAt: number | null;
}

/** Sources whose last attempt failed (old rows are kept server-side; we only show a gentle note). */
export function degradedFeeds<T extends FeedLike>(feeds: T[] | undefined | null): T[] {
  return (feeds ?? []).filter((f) => !f.ok && !!f.lastAttemptAt);
}
