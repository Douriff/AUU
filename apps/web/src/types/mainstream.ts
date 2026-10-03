/** /api/v1/mainstream/* (read-only CEX public data). */
export type MainstreamSeries = { lastTs: number | null; count: number; ageMin: number | null; stale: boolean };

export type MainstreamFreshness = {
  enabled: boolean;
  exchange: string | null;
  exchanges: string[];
  blocked: Record<string, string>;
  symbols: string[];
  lastRefreshMs: number | null;
  lastError: string | null;
  stale: boolean;
  staleSeries: string[];
  gaps: Record<string, number>;
  series: Record<string, MainstreamSeries>;
};

export type MainstreamItem = {
  symbol: string;
  pair: string;
  perp: string;
  price?: number | null;
  priceTs?: number | null;
  change24h?: number | null;
  change30d?: number | null;
  spark1d?: number[];
  fundingLast?: number | null;
  fundingLastTs?: number | null;
  funding7dAvg?: number | null;
  fundingNow?: number | null;
  nextFundingMs?: number | null;
  fundingAnnualized?: number | null;
};

export type MainstreamOverview = {
  exchange: string | null;
  quote: string;
  items: MainstreamItem[];
  freshness: MainstreamFreshness;
};

export type MainstreamCandle = { ts: number; open: number; high: number; low: number; close: number; volume: number };

export type MainstreamTf = "1m" | "5m" | "15m" | "1h" | "4h" | "1d";

export type MainstreamCandles = {
  exchange: string | null;
  symbol: string;
  pair: string;
  tf: string;
  candles: MainstreamCandle[];
  /** 1m/5m/15m only: days of history kept on the server. */
  retentionDays?: number;
  /** Oldest bar the server can serve for this timeframe (ms). */
  oldestAllowed?: number | null;
  /** Paging back reached the oldest available bar. */
  limited?: boolean;
  fetchError?: string;
};

export type MainstreamFunding = { exchange: string | null; symbol: string; perp: string; funding: { ts: number; rate: number }[] };

/** /api/v1/mainstream/paper/* — per-user paper account (paper only). */
export type PaperOrder = {
  id: string;
  clientOrderId: string;
  ts: number;
  symbol: string;
  side: "buy" | "sell";
  type: "market" | "limit";
  qty: number;
  limitPrice: number | null;
  status: "open" | "filled" | "cancelled" | "rejected";
  fillPrice: number | null;
  fillTs: number | null;
  fee: number;
  slippage: number;
  notional: number;
  realized: number;
  reason: string | null;
  mode: "paper";
  duplicate?: boolean;
};

export type PaperPosition = {
  symbol: string;
  qty: number;
  avgPrice: number;
  last: number;
  value: number;
  unrealized: number;
  realized: number;
  fees: number;
  available: number;
};

export type PaperAccount = {
  mode: "paper";
  cash: number;
  availableCash: number;
  startCash: number;
  equity: number;
  pnl: number;
  pnlPct: number;
  positions: PaperPosition[];
  openOrders: PaperOrder[];
  orders: PaperOrder[];
  limits: { maxOrderUsdt: number; maxPositionUsdt: number; minOrderUsdt: number; maxOpenOrders: number; ordersPerMin: number };
  costs: Record<string, { takerFee: number; makerFee: number; slippage: number }>;
  live: { enabled: false; reason: string; message: string };
};

export type PaperOrderRequest = {
  client_order_id: string;
  symbol: string;
  side: "buy" | "sell";
  type: "market" | "limit";
  qty?: number;
  notional?: number;
  limit_price?: number;
};
