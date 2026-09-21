/** Frozen contract field names — mirror of apps/api/app/models/contracts.py */

export interface SymbolInfo {
  symbol: string;
  base: string;
  quote: string;
  kind?: string;
  mint?: string;
}

export interface Candle {
  symbol: string;
  interval: string;
  t: number;
  o: number;
  h: number;
  l: number;
  c: number;
  v: number;
}

export type SignalSide = "long" | "short" | "flat";

export interface SignalOut {
  side: SignalSide;
  strength?: number;
  reason?: string;
  expire_ts?: number;
  tags?: string[];
  /** flattened from SignalEvent */
  strategyId?: string;
  symbol?: string;
  t?: number;
}

export interface RiskOut {
  allow: boolean;
  clipped_size?: number;
  tags: string[];
  notes?: string;
}

export interface Fill {
  ts: number;
  price: number;
  qty: number;
  fee?: number;
  slippage_bps?: number;
  tag?: string;
  symbol?: string;
}

export interface SignalEvent {
  strategyId: string;
  symbol: string;
  t: number;
  signal: SignalOut;
}

export interface RiskEvent {
  strategyId?: string;
  symbol?: string;
  t: number;
  risk: RiskOut;
}

export interface RejectEvent {
  ts: number;
  symbol: string;
  tags: string[];
  notes?: string;
}

export type TradingState = "active" | "reducing" | "halted";

export interface TradingStateEvent {
  state: TradingState;
  reason?: string;
  symbol?: string;
  ts?: number;
}

export type DataSource = "mock" | "paper";

/** Market data provider (env DATA_PROVIDER). Orthogonal to order-path dataSource. */
export type MarketProvider = "mock" | "pumpfun_paper";

export interface PumpCtx {
  curve_progress_bps: number;
  virtual_sol_reserves: string;
  virtual_token_reserves: string;
  real_sol_reserves: string;
  real_token_reserves: string;
  creator_fee_bps: number;
  protocol_fee_bps?: number | null;
  fee_bps?: number | null;
  complete: boolean;
  migrated: boolean;
  amm_pool?: string | null;
}

export interface PumpfunPaperSnapshot {
  mint: string;
  symbol: string;
  phase: "curve" | "graduating" | "amm";
  progress_bps: number;
  complete: boolean;
  migrated: boolean;
  virtual_sol_reserves: string;
  virtual_token_reserves: string;
  real_sol_reserves: string;
  real_token_reserves: string;
  token_total_supply: string;
  price_sol: number;
  price_sol_str?: string;
  market_cap_sol?: number;
  creator_fee_bps?: number;
  pool?: string | null;
  slot?: number;
  updated_ts: number;
  synthetic?: boolean;
}

export interface PumpfunTradeTick {
  mint: string;
  symbol: string;
  ts: number;
  side: "buy" | "sell";
  price: number;
  qty: number;
  sol_amount: number;
  signature?: string;
  phase: "curve" | "amm";
}

export interface BookLevel {
  price: number;
  size: number;
}

export interface BookSnapshot {
  symbol: string;
  bids: BookLevel[];
  asks: BookLevel[];
  mid: number;
  spread_bps: number;
  synthetic?: boolean;
}

export interface TradeTick {
  symbol: string;
  ts: number;
  price: number;
  qty: number;
  side: "buy" | "sell";
  phase?: "curve" | "amm";
}

export interface EnvelopeOk<T> {
  ok: true;
  data: T;
}

export interface EnvelopeErr {
  ok: false;
  error: { code: string; message: string };
}

export type Envelope<T> = EnvelopeOk<T> | EnvelopeErr;
