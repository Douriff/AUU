/**
 * Custom KLineChart overlays + indicators (registered once):
 *  - auuRect / auuText: drawing tools KLineChart does not ship (v10 has no "rect"/"text" overlay)
 *  - auuMarker: ▲▼● signal markers (strategy fills, AUU enter/exit)
 *  - AUU_SCORE / STRAT_MOM: sub-pane indicators fed by our own API data (real data, not demo)
 */
import { registerIndicator, registerOverlay, type IndicatorCreate, type KLineData } from "klinecharts";
import { chartColors } from "@/theme/colorPref";
import { MOM_COLORS } from "@/components/chart/kline/theme";

export type MarkerShape = "arrow" | "dot";
export type MarkerData = { ts: number; shape: MarkerShape; color: string; text: string; side: "low" | "high" };

let done = false;
export function registerAuuKline(): void {
  if (done) return;
  done = true;

  registerOverlay({
    name: "auuRect",
    totalStep: 3,
    needDefaultPointFigure: true,
    needDefaultXAxisFigure: true,
    needDefaultYAxisFigure: true,
    createPointFigures: ({ coordinates }) => {
      if (coordinates.length < 2) return [];
      const [a, b] = coordinates;
      return [
        {
          type: "polygon",
          attrs: { coordinates: [{ x: a.x, y: a.y }, { x: b.x, y: a.y }, { x: b.x, y: b.y }, { x: a.x, y: b.y }] },
          styles: { style: "stroke_fill" },
        },
      ];
    },
  });

  registerOverlay<string>({
    name: "auuText",
    totalStep: 2,
    needDefaultPointFigure: true,
    needDefaultXAxisFigure: false,
    needDefaultYAxisFigure: false,
    createPointFigures: ({ coordinates, overlay }) => {
      if (!coordinates.length) return [];
      const text = typeof overlay.extendData === "string" && overlay.extendData ? overlay.extendData : "…";
      return [{ type: "text", attrs: { x: coordinates[0].x, y: coordinates[0].y, text, baseline: "bottom" }, styles: { style: "stroke_fill" } }];
    },
  });

  registerOverlay<MarkerData>({
    name: "auuMarker",
    totalStep: 1,
    lock: true,
    needDefaultPointFigure: false,
    needDefaultXAxisFigure: false,
    needDefaultYAxisFigure: false,
    createPointFigures: ({ coordinates, overlay }) => {
      const m = overlay.extendData;
      if (!m || !coordinates.length) return [];
      const { x, y } = coordinates[0];
      const below = m.side === "low"; // buy markers sit under the bar, sells above
      const dy = below ? 1 : -1;
      const figs: { type: string; attrs: unknown; styles?: unknown; ignoreEvent?: boolean }[] = [];
      if (m.shape === "dot") figs.push({ type: "circle", attrs: { x, y: y + dy * 9, r: 3.5 }, styles: { style: "fill", color: m.color }, ignoreEvent: true });
      else {
        // tip points at the bar, base is further away
        const tip = y + dy * 5;
        const base = y + dy * 14;
        figs.push({
          type: "polygon",
          attrs: { coordinates: [{ x, y: tip }, { x: x - 5, y: base }, { x: x + 5, y: base }] },
          styles: { style: "fill", color: m.color },
          ignoreEvent: true,
        });
      }
      if (m.text) {
        figs.push({
          type: "text",
          attrs: { x, y: y + dy * 24, text: m.text, align: "center", baseline: "middle" },
          styles: { style: "fill", color: m.color, size: 10, backgroundColor: "transparent", borderSize: 0, paddingLeft: 0, paddingRight: 0, paddingTop: 0, paddingBottom: 0 },
          ignoreEvent: true,
        });
      }
      return figs;
    },
  });

  // ── indicators fed from our API (rows are looked up by bar timestamp) ──
  registerIndicator<{ score?: number }, number, string>({
    name: "AUU_SCORE",
    shortName: "AUU",
    series: "normal",
    precision: 1,
    calcParams: [],
    figures: [],
    calc: (dataList: KLineData[], indicator) => {
      const rows = SCORE_STORE.get(indicator.extendData) ?? {};
      return dataList.map((k) => ({ score: rows[k.timestamp] }));
    },
  });

  registerIndicator<Record<string, number | undefined>, number, string>({
    name: "STRAT_MOM",
    shortName: "TSMOM",
    series: "normal",
    precision: 1,
    calcParams: [],
    figures: [],
    calc: (dataList: KLineData[], indicator) => {
      const rows = MOM_STORE.get(indicator.extendData) ?? {};
      return dataList.map((k) => {
        const r = rows[k.timestamp];
        const o: Record<string, number | undefined> = {};
        if (r) r.forEach((v, i) => (o[`m${i}`] = v == null ? undefined : v * 100));
        return o;
      });
    },
  });
}

const SCORE_STORE = new Map<string, Record<number, number>>();
const MOM_STORE = new Map<string, Record<number, (number | null)[]>>();
let seq = 0;

/** Indicator-create value for the AUU score pane (bars, signed colour follows 红涨绿跌 pref). */
export function auuScoreIndicator(rows: Record<number, number>, title: string): IndicatorCreate {
  const key = `auu${++seq}`;
  SCORE_STORE.set(key, rows);
  return {
    name: "AUU_SCORE",
    shortName: title,
    extendData: key,
    figures: [
      {
        key: "score",
        title: `${title}: `,
        type: "bar",
        baseValue: 0,
        styles: ({ data }: { data: { current?: { score?: number } | null } }) => {
          const v = data.current?.score ?? 0;
          const c = chartColors();
          return { color: v >= 0 ? c.up : c.down, borderColor: v >= 0 ? c.up : c.down };
        },
      },
    ],
  } as unknown as IndicatorCreate;
}

/** Indicator-create value for the daily TSMOM look-back return lines (%). */
export function stratMomIndicator(rows: Record<number, (number | null)[]>, lookbacks: number[], titleOf: (n: number) => string): IndicatorCreate {
  const key = `mom${++seq}`;
  MOM_STORE.set(key, rows);
  return {
    name: "STRAT_MOM",
    shortName: "TSMOM",
    extendData: key,
    figures: lookbacks.map((L, i) => ({ key: `m${i}`, title: `${titleOf(L)}: `, type: "line" })),
    styles: { lines: lookbacks.map((_, i) => ({ color: MOM_COLORS[i % MOM_COLORS.length], style: "solid", size: 1, smooth: false, dashedValue: [2, 2] })) },
  } as unknown as IndicatorCreate;
}

export function releaseIndicatorData(extendData: unknown): void {
  if (typeof extendData !== "string") return;
  SCORE_STORE.delete(extendData);
  MOM_STORE.delete(extendData);
}
