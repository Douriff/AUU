/**
 * Advanced drawing tools registered as KLineChart overlays (registered once from register.ts):
 *  auuFibExt (3 pts)      trend-based Fibonacci extension
 *  auuPitchfork (3 pts)   Andrews pitchfork
 *  auuFibChannel (3 pts)  Fibonacci channel (base line + width point)
 *  auuMeasure (2 pts)     price change, % change, bars, time span in a label box
 *  auuPriceRange (2 pts)  price band with price / % label
 *  auuDateRange (2 pts)   time band with bars / span label
 * Geometry lives in advMath.ts (unit-tested). Points are stored by the normal drawings pipeline (name + points), so
 * localStorage + server sync work unchanged.
 */
import { registerOverlay, type Chart, type Coordinate, type OverlayFigure } from "klinecharts";
import i18n from "i18next";
import { chartColors } from "@/theme/colorPref";
import { fmtFixed } from "@/i18n/format";
import { priceFormat } from "./theme";
import { FIB_CHANNEL, barMsOf, channelLines, fibExtensionLevels, formatSpan, measureStats, pitchfork, type P } from "./advMath";

const ACCENT = "#4c9aff";
const FIB_COLORS = ["#9aa4b2", "#f2c94c", "#26a69a", "#4c9aff", "#bb86fc", "#e91e63", "#ff9800", "#9aa4b2"];

const withAlpha = (hex: string, a: number) =>
  /^#[0-9a-f]{6}$/i.test(hex) ? `rgba(${parseInt(hex.slice(1, 3), 16)},${parseInt(hex.slice(3, 5), 16)},${parseInt(hex.slice(5, 7), 16)},${a})` : hex;

let measureCtx: CanvasRenderingContext2D | null = null;
function textWidth(text: string, size: number): number {
  if (typeof document === "undefined") return text.length * size * 0.6;
  measureCtx ??= document.createElement("canvas").getContext("2d");
  if (!measureCtx) return text.length * size * 0.6;
  measureCtx.font = `${size}px sans-serif`;
  return measureCtx.measureText(text).width;
}

const t = (k: string, o?: Record<string, unknown>) => i18n.t(k, o) as string;
const price = (v: number, ref: number) => fmtFixed(v, priceFormat(Math.abs(ref) || 1).precision);
const signed = (s: string, v: number) => (v > 0 ? `+${s}` : s);

function pts(chart: Chart, overlay: { points: Array<{ timestamp?: number; value?: number }> }, coordinates: Coordinate[], n: number) {
  const p = overlay.points;
  if (coordinates.length < n || p.length < n) return null;
  for (let i = 0; i < n; i++) if (typeof p[i].value !== "number" || typeof p[i].timestamp !== "number") return null;
  const bars = chart.getDataList().map((k) => k.timestamp);
  return { p: p as Array<{ timestamp: number; value: number }>, c: coordinates.slice(0, n) as P[], barMs: barMsOf(bars) };
}

const lineFig = (a: P, b: P, color: string, style: "solid" | "dashed" = "solid", size = 1.2): OverlayFigure => ({
  type: "line",
  attrs: { coordinates: [a, b] },
  styles: { style, color, size, dashedValue: [5, 4] },
});

const labelFig = (x: number, y: number, text: string, color: string, align: "left" | "right" = "left", baseline = "bottom"): OverlayFigure => ({
  type: "text",
  attrs: { x, y, text, align, baseline },
  ignoreEvent: true,
  styles: { style: "fill", color, size: 11, backgroundColor: "transparent", borderSize: 0, paddingLeft: 2, paddingRight: 2, paddingTop: 0, paddingBottom: 0 },
});

/** Rounded label box with one text row per line, placed near (x, y) and kept inside the pane. */
function boxFigs(lines: string[], x: number, y: number, bounding: { width: number; height: number }, color: string): OverlayFigure[] {
  const size = 12;
  const lh = 17;
  const w = Math.ceil(Math.max(...lines.map((l) => textWidth(l, size)))) + 14;
  const h = lines.length * lh + 8;
  let bx = x + 10;
  let by = y - h - 8;
  if (bx + w > bounding.width - 4) bx = x - w - 10;
  if (bx < 2) bx = 2;
  if (by < 2) by = y + 10;
  if (by + h > bounding.height - 2) by = Math.max(2, bounding.height - h - 2);
  const figs: OverlayFigure[] = [
    {
      type: "rect",
      attrs: { x: bx, y: by, width: w, height: h },
      ignoreEvent: true,
      styles: { style: "stroke_fill", color: "rgba(17,22,30,0.92)", borderColor: color, borderSize: 1, borderRadius: 4 },
    },
  ];
  lines.forEach((text, i) =>
    figs.push({
      type: "text",
      attrs: { x: bx + 7, y: by + 4 + lh * i + lh / 2, text, align: "left", baseline: "middle" },
      ignoreEvent: true,
      styles: { style: "fill", color: i === 0 ? color : "#c9d1d9", size, backgroundColor: "transparent", borderSize: 0, paddingLeft: 0, paddingRight: 0, paddingTop: 0, paddingBottom: 0 },
    }),
  );
  return figs;
}

const barsSpanText = (barsN: number, spanMs: number) =>
  `${t("chart.draw.measure.bars", { n: barsN })} · ${formatSpan(spanMs, { d: t("chart.draw.measure.unitD"), h: t("chart.draw.measure.unitH"), m: t("chart.draw.measure.unitM") })}`;

const base = { needDefaultPointFigure: true, needDefaultXAxisFigure: true, needDefaultYAxisFigure: true } as const;

let done = false;
export function registerAdvOverlays(): void {
  if (done) return;
  done = true;

  registerOverlay({
    name: "auuFibExt",
    totalStep: 4,
    ...base,
    createPointFigures: ({ chart, overlay, coordinates, bounding }) => {
      const g = pts(chart, overlay, coordinates, 2);
      if (!g) return [];
      const figs: OverlayFigure[] = [lineFig(g.c[0], g.c[1], ACCENT, "dashed")];
      if (coordinates.length < 3 || overlay.points.length < 3 || typeof overlay.points[2].value !== "number") return figs;
      const c = coordinates[2] as P;
      const [a, b, cc] = [g.p[0].value, g.p[1].value, overlay.points[2].value as number];
      figs.push(lineFig(g.c[1], c, ACCENT, "dashed"));
      const dy = (g.c[1].y - g.c[0].y) / (b - a || 1); // pixels per price unit along the A→B move (sign-correct)
      fibExtensionLevels(a, b, cc).forEach((l, i) => {
        const y = c.y + (l.value - cc) * dy;
        const col = FIB_COLORS[i % FIB_COLORS.length];
        const x0 = Math.min(c.x, g.c[1].x);
        figs.push(lineFig({ x: x0, y }, { x: bounding.width, y }, col, i === 0 ? "dashed" : "solid", 1));
        figs.push(labelFig(x0, y - 1, `${l.ratio} (${price(l.value, cc)})`, col));
      });
      return figs;
    },
  });

  registerOverlay({
    name: "auuPitchfork",
    totalStep: 4,
    ...base,
    createPointFigures: ({ chart, overlay, coordinates, bounding }) => {
      const g = pts(chart, overlay, coordinates, 2);
      if (!g) return [];
      if (coordinates.length < 3) return [lineFig(g.c[0], g.c[1], ACCENT, "dashed")];
      const f = pitchfork(g.c[0], g.c[1], coordinates[2] as P, bounding.width);
      return [
        lineFig(f.handle[0], f.handle[1], "#9aa4b2", "dashed", 1),
        lineFig(f.median[0], f.median[1], ACCENT, "solid", 1.6),
        lineFig(f.upper[0], f.upper[1], "#f2c94c", "solid", 1.2),
        lineFig(f.lower[0], f.lower[1], "#f2c94c", "solid", 1.2),
        {
          type: "polygon",
          attrs: { coordinates: [f.upper[0], f.upper[1], f.lower[1], f.lower[0]] },
          ignoreEvent: true,
          styles: { style: "fill", color: "rgba(76,154,255,0.07)" },
        },
      ];
    },
  });

  registerOverlay({
    name: "auuFibChannel",
    totalStep: 4,
    ...base,
    createPointFigures: ({ chart, overlay, coordinates, bounding }) => {
      const g = pts(chart, overlay, coordinates, 2);
      if (!g) return [];
      if (coordinates.length < 3) return [lineFig(g.c[0], g.c[1], ACCENT, "dashed")];
      const figs: OverlayFigure[] = [];
      channelLines(g.c[0], g.c[1], coordinates[2] as P, bounding.width, FIB_CHANNEL).forEach((l, i) => {
        const col = FIB_COLORS[i % FIB_COLORS.length];
        figs.push(lineFig(l.from, l.to, col, l.ratio === 0 || l.ratio === 1 ? "solid" : "dashed", l.ratio === 0 || l.ratio === 1 ? 1.4 : 1));
        figs.push(labelFig(l.from.x, l.from.y - 1, String(l.ratio), col));
      });
      return figs;
    },
  });

  registerOverlay({
    name: "auuMeasure",
    totalStep: 3,
    ...base,
    createPointFigures: ({ chart, overlay, coordinates, bounding }) => {
      const g = pts(chart, overlay, coordinates, 2);
      if (!g) return [];
      const [a, b] = g.c;
      const m = measureStats(g.p[0].value, g.p[1].value, g.p[0].timestamp, g.p[1].timestamp, g.barMs);
      const col = m.delta >= 0 ? chartColors().up : chartColors().down;
      const pct = m.pct == null ? "" : ` (${signed(fmtFixed(m.pct, 2), m.pct)}%)`;
      return [
        {
          type: "rect",
          attrs: { x: Math.min(a.x, b.x), y: Math.min(a.y, b.y), width: Math.abs(b.x - a.x), height: Math.abs(b.y - a.y) },
          styles: { style: "stroke_fill", color: withAlpha(col, 0.14), borderColor: withAlpha(col, 0.55), borderSize: 1, borderStyle: "dashed", borderDashedValue: [4, 3] },
        },
        lineFig(a, b, col, "solid", 1.5),
        ...boxFigs([`Δ ${signed(price(m.delta, g.p[0].value), m.delta)}${pct}`, barsSpanText(m.bars, m.spanMs)], b.x, b.y, bounding, col),
      ];
    },
  });

  registerOverlay({
    name: "auuPriceRange",
    totalStep: 3,
    ...base,
    createPointFigures: ({ chart, overlay, coordinates, bounding }) => {
      const g = pts(chart, overlay, coordinates, 2);
      if (!g) return [];
      const [a, b] = g.c;
      const m = measureStats(g.p[0].value, g.p[1].value, g.p[0].timestamp, g.p[1].timestamp, g.barMs);
      const col = m.delta >= 0 ? chartColors().up : chartColors().down;
      const x0 = Math.min(a.x, b.x);
      const x1 = Math.max(a.x, b.x);
      const mid = (x0 + x1) / 2;
      const pct = m.pct == null ? "" : ` (${signed(fmtFixed(m.pct, 2), m.pct)}%)`;
      return [
        {
          type: "rect",
          attrs: { x: x0, y: Math.min(a.y, b.y), width: Math.max(1, x1 - x0), height: Math.abs(b.y - a.y) },
          styles: { style: "stroke_fill", color: withAlpha(col, 0.12), borderColor: withAlpha(col, 0.6), borderSize: 1 },
        },
        lineFig({ x: mid, y: a.y }, { x: mid, y: b.y }, col, "solid", 1.3),
        ...boxFigs([`Δ ${signed(price(m.delta, g.p[0].value), m.delta)}${pct}`], mid, Math.min(a.y, b.y), bounding, col),
      ];
    },
  });

  registerOverlay({
    name: "auuDateRange",
    totalStep: 3,
    ...base,
    needDefaultYAxisFigure: false,
    createPointFigures: ({ chart, overlay, coordinates, bounding }) => {
      const g = pts(chart, overlay, coordinates, 2);
      if (!g) return [];
      const [a, b] = g.c;
      const m = measureStats(g.p[0].value, g.p[1].value, g.p[0].timestamp, g.p[1].timestamp, g.barMs);
      const x0 = Math.min(a.x, b.x);
      const x1 = Math.max(a.x, b.x);
      return [
        {
          type: "rect",
          attrs: { x: x0, y: 0, width: Math.max(1, x1 - x0), height: bounding.height },
          styles: { style: "stroke_fill", color: "rgba(76,154,255,0.10)", borderColor: "rgba(76,154,255,0.55)", borderSize: 1 },
        },
        lineFig({ x: x0, y: (a.y + b.y) / 2 }, { x: x1, y: (a.y + b.y) / 2 }, ACCENT, "solid", 1.3),
        ...boxFigs([barsSpanText(m.bars, m.spanMs)], (x0 + x1) / 2, Math.min(a.y, b.y), bounding, ACCENT),
      ];
    },
  });
}

