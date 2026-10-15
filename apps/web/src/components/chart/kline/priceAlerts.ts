/** Price alerts on the chart: group id, helpers and the line style. Server is the source of truth (no localStorage copy). */
import type { PriceAlert, PriceAlertDirection } from "@/providers/HttpWsProvider";

export const ALERT_GROUP = "auu-alerts";
export const ALERT_COLOR = "#f0b90b";
export const ALERT_DONE_COLOR = "#7f8794";
export const DIR_GLYPH: Record<PriceAlertDirection, string> = { above: "↑", below: "↓", cross: "↕" };

/** Round to a sensible number of significant digits (what the user would type). */
export function roundPrice(v: number): number {
  return Number(v.toPrecision(7));
}

/** Parse a user-typed price ("65,000.5", "65000,5" is NOT accepted): positive finite number or null. */
export function parsePrice(raw: string): number | null {
  const s = raw.trim().replace(/[\s,_]/g, "");
  if (!/^\d*\.?\d+(e[+-]?\d+)?$/i.test(s)) return null;
  const n = Number(s);
  return Number.isFinite(n) && n > 0 && n <= 1e9 ? roundPrice(n) : null;
}

/** Default direction for a new alert: where the price has to travel from the last price. */
export function autoDirection(price: number, last: number | null): PriceAlertDirection {
  if (last == null) return "cross";
  return price >= last ? "above" : "below";
}

/** Active alerts for one coin, ready to draw. */
export function lineSpecs(alerts: PriceAlert[], symbol: string): { id: number; price: number; label: string }[] {
  return alerts
    .filter((a) => a.status === "active" && a.symbol === symbol.toUpperCase())
    .map((a) => ({ id: a.id, price: a.price, label: `🔔 ${DIR_GLYPH[a.direction]}${a.note ? ` ${a.note}` : ""}` }));
}
