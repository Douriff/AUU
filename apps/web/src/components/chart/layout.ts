/**
 * Multi-chart layout (1 / 2 / 4 charts). Cell 0 is the "primary" chart: its symbol / timeframe are the page's
 * (URL driven, as before). Cells 1..3 keep their own symbol + timeframe. Persisted in localStorage (per browser
 * profile); pure functions here so they can be unit-tested without a browser (npm run test:layout).
 */
import type { MainstreamTf } from "@/types/mainstream";

export type LayoutN = 1 | 2 | 4;
export type Cell = { symbol: string; tf: MainstreamTf };
export type MultiLayout = { n: LayoutN; extra: [Cell, Cell, Cell]; sync: boolean };

export const LAYOUT_KEY = "auu.kc.layout.v1";
export const TFS: readonly MainstreamTf[] = ["1m", "5m", "15m", "1h", "4h", "1d"];
const SYMBOL_RE = /^[A-Z0-9]{1,15}$/;
const DEFAULT_SYMBOLS = ["ETH", "SOL", "BNB"];

export const isTf = (v: unknown): v is MainstreamTf => typeof v === "string" && (TFS as readonly string[]).includes(v);
export const isLayoutN = (v: unknown): v is LayoutN => v === 1 || v === 2 || v === 4;

export function defaultLayout(tf: MainstreamTf = "1h"): MultiLayout {
  return { n: 1, sync: true, extra: DEFAULT_SYMBOLS.map((symbol) => ({ symbol, tf })) as MultiLayout["extra"] };
}

/** Tolerant parse: anything malformed falls back to defaults field by field (never throws). */
export function parseLayout(raw: string | null | undefined, fallbackTf: MainstreamTf = "1h"): MultiLayout {
  const out = defaultLayout(fallbackTf);
  if (!raw) return out;
  try {
    const j = JSON.parse(raw) as Partial<MultiLayout> | null;
    if (!j || typeof j !== "object") return out;
    if (isLayoutN(j.n)) out.n = j.n;
    if (typeof j.sync === "boolean") out.sync = j.sync;
    if (Array.isArray(j.extra)) {
      for (let i = 0; i < 3; i++) {
        const c = j.extra[i] as Partial<Cell> | undefined;
        if (c && typeof c.symbol === "string" && SYMBOL_RE.test(c.symbol.toUpperCase())) out.extra[i].symbol = c.symbol.toUpperCase();
        if (c && isTf(c.tf)) out.extra[i].tf = c.tf;
      }
    }
  } catch {
    /* corrupt value: defaults */
  }
  return out;
}

export function loadLayout(fallbackTf: MainstreamTf = "1h", kv: Pick<Storage, "getItem"> | null = typeof window !== "undefined" ? window.localStorage : null): MultiLayout {
  try {
    return parseLayout(kv?.getItem(LAYOUT_KEY), fallbackTf);
  } catch {
    return defaultLayout(fallbackTf);
  }
}

export function saveLayout(l: MultiLayout, kv: Pick<Storage, "setItem"> | null = typeof window !== "undefined" ? window.localStorage : null): void {
  try {
    kv?.setItem(LAYOUT_KEY, JSON.stringify({ n: l.n, sync: l.sync, extra: l.extra }));
  } catch {
    /* private mode / quota: layout just does not persist */
  }
}

/** Drop symbols the market list does not know (delisted / typo) so a cell never shows an empty chart. Keeps the cells distinct from the primary where possible. */
export function normalizeCells(l: MultiLayout, known: string[], primary: string): MultiLayout {
  if (!known.length) return l;
  const set = new Set(known);
  const used = new Set([primary]);
  const pool = [...DEFAULT_SYMBOLS, ...known].filter((s) => set.has(s));
  const extra = l.extra.map((c) => {
    let symbol = c.symbol;
    if (!set.has(symbol) || (used.has(symbol) && pool.some((s) => !used.has(s)))) symbol = pool.find((s) => !used.has(s)) ?? known[0];
    used.add(symbol);
    return symbol === c.symbol ? c : { ...c, symbol };
  }) as MultiLayout["extra"];
  return extra.every((c, i) => c === l.extra[i]) ? l : { ...l, extra };
}

export const cellsFor = (l: MultiLayout, primary: Cell): Cell[] => [primary, ...l.extra].slice(0, l.n);
