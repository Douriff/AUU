/** Drawing tools: catalogue + localStorage persistence (the local copy is the offline cache; drawingsSync.ts mirrors it to the server). */
import type { Chart, OverlayCreate, Overlay } from "klinecharts";

export type ToolId = "segment" | "rayLine" | "horizontalStraightLine" | "verticalStraightLine" | "fibonacciLine" | "auuRect" | "parallelStraightLine" | "auuText" | AdvToolId;
export type AdvToolId = "auuFibExt" | "auuPitchfork" | "auuFibChannel" | "auuMeasure" | "auuPriceRange" | "auuDateRange";

/** glyph = compact icon text, i18n key = chart.draw.<id> */
export const DRAW_TOOLS: { id: ToolId; glyph: string; adv?: boolean }[] = [
  { id: "segment", glyph: "╱" },
  { id: "rayLine", glyph: "↗" },
  { id: "horizontalStraightLine", glyph: "─" },
  { id: "verticalStraightLine", glyph: "│" },
  { id: "fibonacciLine", glyph: "Fib" },
  { id: "auuRect", glyph: "▭" },
  { id: "parallelStraightLine", glyph: "⫽" },
  { id: "auuText", glyph: "T" },
  // advanced tools (step 5): 3-point Fib extension / pitchfork / Fib channel, 2-point measure / ranges
  { id: "auuFibExt", glyph: "Fx", adv: true },
  { id: "auuPitchfork", glyph: "Pf", adv: true },
  { id: "auuFibChannel", glyph: "Fc", adv: true },
  { id: "auuMeasure", glyph: "📏", adv: true },
  { id: "auuPriceRange", glyph: "⇕", adv: true },
  { id: "auuDateRange", glyph: "⇔", adv: true },
];

/** points the user has to place (drives the hint text) */
export const TOOL_POINTS: Partial<Record<ToolId, number>> = { auuFibExt: 3, auuPitchfork: 3, auuFibChannel: 3, auuMeasure: 2, auuPriceRange: 2, auuDateRange: 2 };

export const USER_GROUP = "user";
export const drawingsKey = (symbol: string) => `auu.kc.drawings.v1.${symbol.toUpperCase()}`;
const key = drawingsKey;

export type Saved = { name: string; points: { timestamp: number; value: number }[]; extendData?: unknown; styles?: unknown };

export function loadDrawings(symbol: string): Saved[] {
  try {
    const raw = JSON.parse(window.localStorage.getItem(key(symbol)) || "[]");
    return Array.isArray(raw) ? raw.filter((o) => o && typeof o.name === "string" && Array.isArray(o.points)) : [];
  } catch {
    return [];
  }
}

export function saveDrawings(chart: Chart, symbol: string): number {
  const list: Saved[] = chart
    .getOverlays({ groupId: USER_GROUP })
    .filter((o: Overlay) => (o as unknown as { currentStep: number }).currentStep === -1 && (o.points?.length ?? 0) > 0)
    .map((o) => ({
      name: o.name,
      points: (o.points ?? []).filter((p) => typeof p.timestamp === "number" && typeof p.value === "number").map((p) => ({ timestamp: p.timestamp as number, value: p.value as number })),
      extendData: typeof o.extendData === "string" ? o.extendData : undefined,
      styles: o.styles ?? undefined,
    }));
  try {
    window.localStorage.setItem(key(symbol), JSON.stringify(list));
  } catch {
    /* private mode */
  }
  return list.length;
}

export type OverlayHandlers = Pick<OverlayCreate, "onDrawEnd" | "onPressedMoveEnd" | "onRemoved" | "onSelected" | "onDeselected" | "onDoubleClick">;
