/** Public-quote refresh cadence (step 0: polling and cache, no new push channel).
 *
 *  While a tab is visible, polls stay short enough that a price on screen is only a couple of
 *  seconds behind the server cache. A hidden tab slows down. Coming back (visibility or window
 *  focus) fetches immediately instead of waiting out the previous interval.
 *  Numbers are asserted by `npm run test:quote`.
 */

/** Mainstream list + open-coin header while the tab is visible and push is not live. Was 5s. */
export const MARKET_POLL_MS = 2_000;
/** Safety poll while /api/v1/ws push is live. Push still moves the price; this only recalibrates. Was 30s. */
export const MARKET_POLL_LIVE_MS = 12_000;
/** Background tab heartbeat so a throttled timer is not the only way back to a fresh quote. */
export const MARKET_POLL_HIDDEN_MS = 20_000;

/** Forming-bar poll while visible and that series is not being pushed. Was 10s. */
export const CHART_POLL_MS = 2_000;
/** Forming-bar safety poll while that series is pushed. Was 30s. */
export const CHART_POLL_LIVE_MS = 12_000;
export const CHART_POLL_HIDDEN_MS = 20_000;

/** Majors board (top-100 tickers). Was 5s, against an 8s server cache. */
export const MAJORS_POLL_MS = 4_000;
export const MAJORS_POLL_HIDDEN_MS = 20_000;

/** Display-only order book. Was 3s. */
export const BOOK_POLL_MS = 2_000;

/** Funding history stays on a ~30s cadence at MARKET_POLL_MS (15 × 2s). */
export const SLOW_EVERY = 15;

export type PollWhy = "timer" | "show";

export interface VisiblePollOpts {
  /** Cadence while the document is visible. Read each time the timer is armed. */
  visibleMs: number | (() => number);
  /** Cadence while hidden. */
  hiddenMs: number | (() => number);
  /** ``show`` = the tab just became visible or the window regained focus (do not wait). */
  run: (why: PollWhy) => void;
  isHidden: () => boolean;
  onVisibility: (cb: () => void) => () => void;
  onFocus?: (cb: () => void) => () => void;
  now?: () => number;
  /** A visibility change and a focus event often arrive together; only the first fetches. */
  kickCoalesceMs?: number;
  setTimer?: (fn: () => void, ms: number) => unknown;
  clearTimer?: (id: unknown) => void;
}

function readMs(v: number | (() => number)): number {
  const n = typeof v === "function" ? v() : v;
  return n > 0 ? n : 1_000;
}

/** Poll ``run`` on the visible cadence, slower while hidden, and immediately when shown again.
 *  Does not call ``run`` at subscribe time (the caller already loaded). Returns a disposer. */
export function createVisiblePoll(o: VisiblePollOpts): () => void {
  const setT = o.setTimer ?? ((fn, ms) => globalThis.setTimeout(fn, ms));
  const clearT = o.clearTimer ?? ((id) => globalThis.clearTimeout(id as ReturnType<typeof setTimeout>));
  const now = o.now ?? (() => Date.now());
  const coalesce = o.kickCoalesceMs ?? 400;
  let timer: unknown = null;
  let armedHidden = false;
  // The caller fetched as it subscribed; a focus event in the same moment must not fetch again.
  let lastRun = now();

  const stop = () => {
    if (timer != null) clearT(timer);
    timer = null;
  };
  const arm = () => {
    stop();
    const hidden = o.isHidden();
    const ms = readMs(hidden ? o.hiddenMs : o.visibleMs);
    armedHidden = hidden;
    timer = setT(() => {
      // A short visible timer can fire after the tab was hidden (no event yet): do not fetch.
      if (o.isHidden() && !armedHidden) {
        arm();
        return;
      }
      lastRun = now();
      o.run("timer");
      arm();
    }, ms);
  };
  const kick = () => {
    if (o.isHidden()) {
      arm();
      return;
    }
    const t = now();
    if (t - lastRun < coalesce) {
      arm();
      return;
    }
    lastRun = t;
    o.run("show");
    arm();
  };
  const offVis = o.onVisibility(() => {
    if (o.isHidden()) arm();
    else kick();
  });
  const offFocus = o.onFocus
    ? o.onFocus(() => {
        if (!o.isHidden()) kick();
      })
    : () => {};
  arm();
  return () => {
    stop();
    offVis();
    offFocus();
  };
}

/** Age of a quote without mixing the server clock and the client clock.
 *  ``serverNow - eventAt`` is how old the sample already was when the response was built;
 *  ``clientNow - clientFetchedAt`` is how long the response has been on screen. */
export function skewFreeAgeMs(
  eventAt: number | null | undefined,
  serverNow: number | null | undefined,
  clientFetchedAt: number,
  clientNow: number,
): number {
  if (!clientFetchedAt) return 0;
  const sinceFetch = Math.max(0, clientNow - clientFetchedAt);
  if (eventAt == null || serverNow == null || !(serverNow > 0)) return sinceFetch;
  return Math.max(0, serverNow - eventAt) + sinceFetch;
}

export function quoteAgeKey(ageMs: number): { key: "common.updatedNow" | "common.updatedAgo" | "common.updatedMin"; n?: number } {
  const s = Math.max(0, Math.round(ageMs / 1000));
  if (s < 2) return { key: "common.updatedNow" };
  if (s < 60) return { key: "common.updatedAgo", n: s };
  return { key: "common.updatedMin", n: Math.floor(s / 60) };
}

export interface QuoteSample {
  eventAt: number | null;
  serverNow: number | null;
  clientFetchedAt: number;
  /** Exchange ticker unavailable: the page is showing saved candles, not a live quote. */
  store?: boolean;
}

export function tickerSample(source: string | undefined, at: number | null | undefined, asOf: number, clientFetchedAt: number): QuoteSample {
  if (source !== "ticker" || at == null) return { eventAt: null, serverNow: null, clientFetchedAt, store: true };
  return { eventAt: at, serverNow: asOf, clientFetchedAt, store: false };
}

/** A push frame is stamped with the client clock (``recvAt``), so event and "server now" are the same instant. */
export function pushSample(recvAt: number): QuoteSample {
  return { eventAt: recvAt, serverNow: recvAt, clientFetchedAt: recvAt, store: false };
}
