/** First-visit splash. Key is local so clearing site data re-shows it. */
export const SPLASH_SEEN_KEY = "auu.splash.seen";

export function splashSeen(): boolean {
  try {
    return window.localStorage.getItem(SPLASH_SEEN_KEY) === "1";
  } catch {
    return true; // storage blocked → skip splash
  }
}

export function markSplashSeen(): void {
  try {
    window.localStorage.setItem(SPLASH_SEEN_KEY, "1");
  } catch {
    /* ignore */
  }
}

export function prefersReducedMotion(): boolean {
  try {
    return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  } catch {
    return false;
  }
}

export function isNarrowMobile(): boolean {
  try {
    return window.matchMedia("(max-width: 720px)").matches;
  } catch {
    return false;
  }
}
