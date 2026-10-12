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
