/** Pump.fun paper venue — keep in sync with apps/api health / pumpfun_paper. */
export const VENUE = "Pump.fun";
export const QUOTE = "SOL";
export const DEFAULT_SYMBOL = "PUMPDEMO/SOL";

export const DATA_SOURCES = ["mock", "paper", "pumpfun_paper"] as const;
export type DataSource = (typeof DATA_SOURCES)[number];

export function isPaperPath(src: DataSource): boolean {
  return src === "paper" || src === "pumpfun_paper";
}

export function truncateMint(mint: string, head = 8, tail = 4): string {
  if (mint.length <= head + tail + 1) return mint;
  return `${mint.slice(0, head)}…${mint.slice(-tail)}`;
}
