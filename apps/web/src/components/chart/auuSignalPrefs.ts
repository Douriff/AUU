/** Persisted AUU proprietary signal overlay toggle (experimental). Parallel to indicatorPrefs. */

const KEY = "auu.ms.auuSignal";

/** Default off — experimental; user opts in. */
export const DEFAULT_AUU_SIGNAL_ON = false;

export function loadAuuSignalPref(): boolean {
  try {
    const raw = window.localStorage.getItem(KEY);
    if (raw == null) return DEFAULT_AUU_SIGNAL_ON;
    if (raw === "1" || raw === "true") return true;
    if (raw === "0" || raw === "false") return false;
    const parsed = JSON.parse(raw) as { on?: boolean };
    return typeof parsed?.on === "boolean" ? parsed.on : DEFAULT_AUU_SIGNAL_ON;
  } catch {
    return DEFAULT_AUU_SIGNAL_ON;
  }
}

export function saveAuuSignalPref(on: boolean): void {
  try {
    window.localStorage.setItem(KEY, JSON.stringify({ on }));
  } catch {
    /* private mode */
  }
}

const REGIME_KEY = "auu.ms.auuRegime";

/** Regime shading (long/short bands behind the candles) — default on once the AUU signal is on. */
export function loadAuuRegimePref(): boolean {
  try {
    return window.localStorage.getItem(REGIME_KEY) !== "0";
  } catch {
    return true;
  }
}

export function saveAuuRegimePref(on: boolean): void {
  try {
    window.localStorage.setItem(REGIME_KEY, on ? "1" : "0");
  } catch {
    /* private mode */
  }
}
