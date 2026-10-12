/** Splash prefs — no persisted "seen" gate; every full page load shows intro. */

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
