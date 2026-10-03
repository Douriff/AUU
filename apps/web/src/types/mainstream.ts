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

/** M3 daily paper runner (GET /api/v1/mainstream/strategy). */
export interface StrategyCurvePoint {
  day: string;
  ts: number;
  nav: number;
  strategy: number;
  ret: number;
  cost: number;
  funding: number;
  gross: number;
  btc: number | null;
  tbill: number;
  catchup: boolean;
}
export interface StrategyPosition {
  coin: string;
  weight: number;
  notional: number;
  price: number | null;
  qty: number | null;
  target: number | null;
}
export interface StrategyFill {
  day: number;
  coin: string;
  side: "buy" | "sell";
  w_from: number;
  w_to: number;
  notional: number;
  qty: number;
  price: number;
  fill_price: number;
  fee: number;
  slippage: number;
}
export interface StrategyStatus {
  active: boolean;
  strategy?: string;
  lastDay: string | null;
  lastRunAt?: number | null;
  nextDueDay?: string;
  hoursSinceRebalance: number;
  stallHours: number;
  stalled: boolean;
  reason: string;
  waiting?: string;
  lastError?: string;
  days: number;
}
export interface StrategyGoNoGo {
  standard: string;
  verdict: "pending" | "go" | "no-go";
  lamp: string;
  days: number;
  minDays: number;
  message: string;
  tbill: number;
  stats?: Record<string, number | null>;
}
export interface StrategyUniverse {
  configured?: string[];
  exchange?: string | null;
  coverage?: Record<string, { firstDay: string; lastDay: string; days: number; fundingFrom: string | null; fundingRows: number }>;
  unavailable?: Record<string, string>;
  current: string[];
  changes: { day: string; coins: string[]; prev: string[]; recordedAt: number; note: string | null }[];
  survivorship: string;
  error?: string;
}
export interface StrategySummary {
  universe: StrategyUniverse;
  strategy: { name: string; lookbacks: number[]; target_vol: number; cap: number; long_only: boolean };
  mode: "paper";
  live: { enabled: false; reason: string; message: string };
  startNav: number;
  nav: number;
  cost: { taker: number; slippage: Record<string, number>; slippageDefault: number; band: number; funding: string };
  curve: StrategyCurvePoint[];
  positions: StrategyPosition[];
  fills: StrategyFill[];
  totals: { strategy?: number; btc?: number | null; tbill?: number };
  goNoGo: StrategyGoNoGo;
  status: StrategyStatus;
  risk?: StrategyRisk;
  execShadow?: ExecShadowSummary | null;
  expectedBand?: ExpectedBand | null;
  version?: RunVersion;
}
export interface ExpectedBand {
  status: "inside" | "below" | "above" | "dd_breach" | "beyond_horizon" | "no_data" | "no_band" | "error";
  label?: string;
  name?: string;
  version?: number;
  registered?: { seed: number; block: number; n_paths: number; horizon?: number };
  sourceSha?: string;
  horizon?: number;
  n?: number;
  cum?: number;
  mdd?: number;
  p05?: number;
  p50?: number;
  p95?: number;
  mddP05?: number;
  outside?: boolean;
  curve?: { n: number; p05: number; p50: number; p95: number; paper: number | null; day: string | null }[];
  note?: string;
  error?: string;
}
export interface RunVersionHashes {
  git_commit: string | null;
  params_sha: string | null;
  cost_model_sha: string | null;
}
export interface RunVersion {
  current: RunVersionHashes;
  lastRun: RunVersionHashes;
  distinct: Record<string, number>;
  unversionedRuns: number;
  changedSinceLastRun: boolean;
}
export interface ExecShadowSummary {
  ledger: string;
  n: number;
  skipped: number;
  errors: number;
  partial: number;
  notionalWeightedDeviationBp: number | null;
  notionalWeightedShortfallMidBp: number | null;
  coins: { coin: string; n: number; notional: number; spreadBp: number; shortfallMidBp: number; shortfallCloseBp: number | null; assumedBp: number; deviationBp: number }[];
  note: string;
  error?: string;
}
export interface StrategyRiskEvent {
  ts: number;
  day: string | null;
  kind: string;
  label: string;
  action: string;
  value: number | null;
  threshold: number | null;
  detail: Record<string, unknown>;
  at: string;
}
export interface StrategyRisk {
  enabled: boolean;
  limits?: Record<string, number>;
  locked?: boolean;
  lock?: { kind: "24h" | "review"; since: number; until?: number; reason: string } | null;
  dataBad?: { since: number; reason: string } | null;
  todayFlags?: { stop_new?: boolean; halve?: boolean; flat?: boolean };
  lastMark?: { ts: number; nav: number; dayRet: number; drawdown: number; dataBad: boolean; reason: string } | null;
  events?: StrategyRiskEvent[];
  eventCount?: number;
  adjustments?: { ts: number; kinds: string; navMark: number; navAfter: number; cost: number; turnover: number }[];
  backtestNote?: string;
}

/** Shadow hypothesis S3 (GET /api/v1/mainstream/shadow/s3): no capital, not evidence. */
export interface ShadowTrade {
  id: number;
  coin: string;
  signal_ts: number;
  rate: number;
  interval_h: number;
  f8: number;
  entry_bar: number;
  exit_bar: number;
  status: "pending" | "open" | "closed" | "void";
  entry_px: number | null;
  exit_px: number | null;
  gross: number | null;
  funding: number | null;
  cost: number | null;
  net: number | null;
  note: string | null;
}
export interface ShadowS3Summary {
  label: string;
  capital: number;
  ledger: string;
  rule: Record<string, string | number>;
  ruleHash: string;
  ruleFrozen: boolean;
  registeredAt: number;
  registeredDay: string;
  source: string | null;
  counts: { pending: number; open: number; closed: number; void: number };
  evalAt: number;
  progress: number;
  running: { n: number; mean_net_bps: number; win: number; sum_net: number } | null;
  runningNote: string;
  evaluations: { milestone: number; ts: number; n: number; verdict: string; mean_net_bps: number; ci_bps: [number, number]; win: number; ex_best3_bps: number | null }[];
  trades: ShadowTrade[];
  notes: string[];
  lastError: string;
}
