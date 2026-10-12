/** Persisted K-line indicator toggles (mainstream trade chart). */

export type IndicatorId = "ma" | "ema" | "boll" | "macd" | "rsi" | "kdj";

export type IndicatorPrefs = Record<IndicatorId, boolean>;

export const INDICATOR_IDS: IndicatorId[] = ["ma", "ema", "boll", "macd", "rsi", "kdj"];

/** Default: MA(7/25/99) on — the most common overlay on CEX trade charts. */
export const DEFAULT_INDICATOR_PREFS: IndicatorPrefs = {
  ma: true,
  ema: false,
  boll: false,
  macd: false,
  rsi: false,
  kdj: false,
};

const KEY = "auu.ms.indicators";

export function loadIndicatorPrefs(): IndicatorPrefs {
  try {
    const raw = window.localStorage.getItem(KEY);
    if (!raw) return { ...DEFAULT_INDICATOR_PREFS };
    const parsed = JSON.parse(raw) as Partial<IndicatorPrefs>;
    const next = { ...DEFAULT_INDICATOR_PREFS };
    for (const id of INDICATOR_IDS) {
      if (typeof parsed[id] === "boolean") next[id] = parsed[id]!;
    }
    return next;
  } catch {
    return { ...DEFAULT_INDICATOR_PREFS };
  }
}

export function saveIndicatorPrefs(p: IndicatorPrefs): void {
  try {
    window.localStorage.setItem(KEY, JSON.stringify(p));
  } catch {
    /* private mode */
  }
}

export function toggleIndicator(p: IndicatorPrefs, id: IndicatorId): IndicatorPrefs {
  return { ...p, [id]: !p[id] };
}
