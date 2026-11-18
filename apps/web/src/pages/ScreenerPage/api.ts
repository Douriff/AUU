import { ApiError, AUTH_REQUIRED_EVENT } from "@/providers/HttpWsProvider";

function apiBase(): string {
  const env = import.meta.env.VITE_API_BASE;
  if (env) return String(env).replace(/\/$/, "");
  return "";
}

export type MetricKey = "price" | "quoteVolume24h" | "ema10" | "ema20" | "ema200" | "rsi14";
export type Cmp = "gt" | "gte" | "lt" | "lte";
export type Metric =
  | { field: "price" }
  | { field: "quoteVolume24h" }
  | { field: "ema"; period: 10 | 20 | 200; tf: "1d" }
  | { field: "rsi"; period: 14; tf: "1d" };
export type Leaf = { cmp: Cmp; left: Metric; right: Metric | { num: number } };
export type Query = { op: "all" | "any"; of: Leaf[] };

export type Reading = { state: string; value: number | null; usable: boolean; display?: number };

export type ScreenRow = {
  base: string;
  symbol: string;
  chartPath: string;
  verdict: "pass" | "fail" | "unknown";
  eval: "true" | "false" | "unknown";
  values: Record<string, Reading>;
  closedBars: number | null;
  lastClosedOpenTime: number | null;
  lastClosedCloseTime: number | null;
  formingOpenTime: number | null;
  formingExcluded: boolean;
  klineSource: string | null;
  klineFetchedAt: number | null;
  priceSourceTime: number | null;
  reasons: string[];
};

export type Screen = {
  liveEnabled: boolean;
  mode: string;
  orders: boolean;
  orderMode: string;
  fixture: boolean;
  status: "ok" | "degraded" | "unavailable";
  universe: { id: string; listed: number; rejected: number; fullMarket: boolean; priceNearOneExcluded: boolean; topN: null; stale: boolean };
  candlePolicy: { indicatorTf: string; indicatorsUse: string; latestPrice: string; formingExcluded: boolean };
  asOf: {
    serverNow: number;
    sourceKind: string;
    venue: string;
    tickerFetchedAt: number | null;
    tickerSourceTime: number | null;
    tickerState: string;
    tickerError: string | null;
    infoFetchedAt: number | null;
    infoState: string;
    infoError: string | null;
  };
  coverage: { needed: number; ready: number; pending: number };
  counts: { passes: number; fails: number; unknown: number };
  rows: ScreenRow[];
};

export type ScreenMeta = {
  fixture: boolean;
  liveEnabled: boolean;
  sourceKind: string;
  example: Query;
};

type Envelope<T> = { ok: true; data: T } | { ok: false; error?: { code?: string; message?: string; key?: string } };

async function post<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`${apiBase()}${path}`, {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (res.status === 401 && typeof window !== "undefined") {
    window.dispatchEvent(new Event(AUTH_REQUIRED_EVENT));
  }
  const env = (await res.json()) as Envelope<T>;
  if (!env.ok) throw new ApiError(env.error, res.status);
  return env.data;
}

async function get<T>(path: string): Promise<T> {
  const res = await fetch(`${apiBase()}${path}`, { credentials: "include" });
  if (res.status === 401 && typeof window !== "undefined") {
    window.dispatchEvent(new Event(AUTH_REQUIRED_EVENT));
  }
  const env = (await res.json()) as Envelope<T>;
  if (!env.ok) throw new ApiError(env.error, res.status);
  return env.data;
}

const flights = new Map<string, Promise<Screen>>();

/** Identical in-flight screens share one request. */
export function runScreen(query: Query): Promise<Screen> {
  const key = JSON.stringify(query);
  const hit = flights.get(key);
  if (hit) return hit;
  const pending = post<Screen>("/api/v1/screener/spot", query).finally(() => {
    if (flights.get(key) === pending) flights.delete(key);
  });
  flights.set(key, pending);
  return pending;
}

export function loadMeta(): Promise<ScreenMeta> {
  return get<ScreenMeta>("/api/v1/screener/spot");
}

export async function downloadCsv(query: Query): Promise<void> {
  const res = await fetch(`${apiBase()}/api/v1/screener/spot/csv`, {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(query),
  });
  if (!res.ok) {
    let code = "BAD_REQUEST";
    try {
      const env = (await res.json()) as Envelope<never>;
      if (!env.ok) throw new ApiError(env.error, res.status);
    } catch (err) {
      if (err instanceof ApiError) throw err;
      throw new ApiError({ code }, res.status);
    }
  }
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = "auu-spot-screener.csv";
  link.rel = "noopener";
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

export function metricOf(key: MetricKey): Metric {
  if (key === "price") return { field: "price" };
  if (key === "quoteVolume24h") return { field: "quoteVolume24h" };
  if (key === "rsi14") return { field: "rsi", period: 14, tf: "1d" };
  if (key === "ema10") return { field: "ema", period: 10, tf: "1d" };
  if (key === "ema20") return { field: "ema", period: 20, tf: "1d" };
  return { field: "ema", period: 200, tf: "1d" };
}

export function parseThreshold(raw: string): number | null {
  const text = raw.trim();
  if (!/^[+-]?(?:\d+\.?\d*|\d*\.\d+)(?:[eE][+-]?\d+)?$/.test(text)) return null;
  const value = Number(text);
  if (!Number.isFinite(value) || Math.abs(value) > 1e15) return null;
  return value;
}

/** Only an allowlisted base may be linked to the existing chart route. */
export function chartHref(base: string): string | null {
  if (!/^[A-Z0-9]{2,20}$/.test(base)) return null;
  return `/?symbol=${base}&venue=binance`;
}
