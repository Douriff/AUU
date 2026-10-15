/** Indicator catalogue + persisted toggles/params for the KLineChart-based trade chart. */
import type { Chart } from "klinecharts";

export type IndicatorId = "ma" | "ema" | "boll" | "sar" | "vol" | "macd" | "rsi" | "kdj" | "obv" | "cci" | "dmi" | "wr";

export type IndicatorDef = {
  id: IndicatorId;
  /** KLineChart built-in indicator name */
  name: string;
  /** drawn on the candle pane (true) or in its own sub pane */
  main: boolean;
  defaults: number[];
  /** per-parameter bounds [min, max, step] */
  bounds: [number, number, number][];
  /** parameter labels (not translated: standard TA terms) */
  labels: string[];
};

export const INDICATOR_DEFS: IndicatorDef[] = [
  { id: "ma", name: "MA", main: true, defaults: [7, 25, 99], bounds: [[1, 500, 1], [1, 500, 1], [1, 500, 1]], labels: ["MA1", "MA2", "MA3"] },
  { id: "ema", name: "EMA", main: true, defaults: [12, 26], bounds: [[1, 500, 1], [1, 500, 1]], labels: ["EMA1", "EMA2"] },
  { id: "boll", name: "BOLL", main: true, defaults: [20, 2], bounds: [[2, 200, 1], [0.5, 5, 0.5]], labels: ["N", "σ"] },
  { id: "sar", name: "SAR", main: true, defaults: [2, 2, 20], bounds: [[1, 10, 1], [1, 10, 1], [5, 50, 1]], labels: ["Start", "Step", "Max"] },
  { id: "vol", name: "VOL", main: false, defaults: [5, 10, 20], bounds: [[1, 200, 1], [1, 200, 1], [1, 200, 1]], labels: ["MA1", "MA2", "MA3"] },
  { id: "macd", name: "MACD", main: false, defaults: [12, 26, 9], bounds: [[1, 100, 1], [2, 200, 1], [1, 100, 1]], labels: ["Fast", "Slow", "Signal"] },
  { id: "rsi", name: "RSI", main: false, defaults: [14], bounds: [[2, 100, 1]], labels: ["N"] },
  { id: "kdj", name: "KDJ", main: false, defaults: [9, 3, 3], bounds: [[2, 100, 1], [1, 20, 1], [1, 20, 1]], labels: ["N", "M1", "M2"] },
  { id: "obv", name: "OBV", main: false, defaults: [30], bounds: [[2, 200, 1]], labels: ["MA"] },
  { id: "cci", name: "CCI", main: false, defaults: [20], bounds: [[2, 200, 1]], labels: ["N"] },
  { id: "dmi", name: "DMI", main: false, defaults: [14, 6], bounds: [[2, 100, 1], [1, 50, 1]], labels: ["N", "ADXR"] },
  { id: "wr", name: "WR", main: false, defaults: [14], bounds: [[2, 100, 1]], labels: ["N"] },
];

export const INDICATOR_IDS = INDICATOR_DEFS.map((d) => d.id);
/** quick-toggle chips on the chart footer (the original six); everything else lives in the panel */
export const QUICK_IDS: IndicatorId[] = ["ma", "ema", "boll", "macd", "rsi", "kdj"];

export type IndicatorPrefs = { on: Record<IndicatorId, boolean>; params: Record<IndicatorId, number[]> };

export const defaultPrefs = (): IndicatorPrefs => ({
  on: Object.fromEntries(INDICATOR_DEFS.map((d) => [d.id, d.id === "ma" || d.id === "vol"])) as Record<IndicatorId, boolean>,
  params: Object.fromEntries(INDICATOR_DEFS.map((d) => [d.id, [...d.defaults]])) as Record<IndicatorId, number[]>,
});

/** multi-chart cells start light: candles + MA only (no sub panes), session-only */
export const compactPrefs = (): IndicatorPrefs => {
  const p = defaultPrefs();
  p.on.vol = false;
  return p;
};

const KEY = "auu.ms.indicators.v2";
const OLD_KEY = "auu.ms.indicators"; // v1: boolean map of the original six

function clampParams(def: IndicatorDef, raw: unknown): number[] {
  const out = [...def.defaults];
  if (Array.isArray(raw)) {
    def.defaults.forEach((_, i) => {
      const n = Number(raw[i]);
      if (Number.isFinite(n)) out[i] = Math.min(def.bounds[i][1], Math.max(def.bounds[i][0], n));
    });
  }
  return out;
}

export function loadPrefs(): IndicatorPrefs {
  const base = defaultPrefs();
  try {
    const raw = window.localStorage.getItem(KEY);
    if (raw) {
      const p = JSON.parse(raw) as Partial<IndicatorPrefs>;
      for (const d of INDICATOR_DEFS) {
        if (typeof p.on?.[d.id] === "boolean") base.on[d.id] = p.on[d.id];
        base.params[d.id] = clampParams(d, p.params?.[d.id]);
      }
      return base;
    }
    const old = window.localStorage.getItem(OLD_KEY);
    if (old) {
      const o = JSON.parse(old) as Record<string, unknown>;
      for (const id of ["ma", "ema", "boll", "macd", "rsi", "kdj"] as IndicatorId[]) if (typeof o[id] === "boolean") base.on[id] = o[id] as boolean;
    }
  } catch {
    /* ignore */
  }
  return base;
}

export function savePrefs(p: IndicatorPrefs): void {
  try {
    window.localStorage.setItem(KEY, JSON.stringify(p));
  } catch {
    /* private mode */
  }
}

export function sanitizeParam(def: IndicatorDef, i: number, v: number): number {
  const [lo, hi] = def.bounds[i];
  if (!Number.isFinite(v)) return def.defaults[i];
  return Math.min(hi, Math.max(lo, v));
}

/** Make the chart match prefs: create / remove / re-parameterise indicators. `ids` tracks created ones. */
export function syncIndicators(chart: Chart, prefs: IndicatorPrefs, ids: Map<IndicatorId, { id: string; params: string }>): void {
  for (const def of INDICATOR_DEFS) {
    const have = ids.get(def.id);
    const params = JSON.stringify(prefs.params[def.id]);
    if (!prefs.on[def.id]) {
      if (have) {
        chart.removeIndicator({ id: have.id });
        ids.delete(def.id);
      }
      continue;
    }
    if (have && have.params === params) continue;
    if (have) {
      chart.overrideIndicator({ name: def.name, id: have.id, calcParams: prefs.params[def.id] } as never);
      ids.set(def.id, { id: have.id, params });
      continue;
    }
    const id = def.main
      ? chart.createIndicator({ name: def.name, calcParams: prefs.params[def.id], paneId: "candle_pane" }, true)
      : chart.createIndicator({ name: def.name, calcParams: prefs.params[def.id] }, false);
    if (id) ids.set(def.id, { id, params });
  }
}

/** Keep the candle pane readable: shrink sub panes as more of them are on (and also when STRAT_MOM / AUU_SCORE are shown). */
export function layoutPanes(chart: Chart): void {
  const subs = new Set<string>();
  for (const ind of chart.getIndicators()) if (ind.paneId && ind.paneId !== "candle_pane") subs.add(ind.paneId);
  const n = subs.size;
  if (!n) return;
  const total = chart.getSize()?.height ?? 480;
  const each = Math.max(56, Math.min(110, Math.floor((total * 0.55) / n)));
  for (const id of subs) chart.setPaneOptions({ id, height: each });
}
