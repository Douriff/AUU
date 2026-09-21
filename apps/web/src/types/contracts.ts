/** Frozen contract field names — mirror of apps/api/app/models/contracts.py */

export interface SymbolInfo {
  symbol: string;
  base: string;
  quote: string;
  kind?: string;
  mint?: string;
  venue?: string;
  curve_progress?: number;
  virtual_sol_reserves?: number;
  virtual_token_reserves?: number;
  graduated?: boolean;
  migrated?: boolean;
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
}

export interface TradeTick {
  symbol: string;
  ts: number;
  price: number;
  qty: number;
  side: "buy" | "sell";
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

export interface CurveSnapshot {
  symbol: string;
  mint?: string;
  base?: string;
  venue?: string;
  quote?: string;
  virtual_sol_reserves?: number;
  virtual_token_reserves?: number;
  real_sol_reserves?: number;
  curve_progress?: number;
  graduated?: boolean;
  migrated?: boolean;
  price_sol?: number;
  graduation_sol?: number;
}

/** Additive paper-path types — frozen Fill / RiskOut / SignalOut names unchanged. */

export type OrderSide = "buy" | "sell";

export interface SizeIn {
  target_notional: number;
  max_slippage_bps?: number;
  urgency?: "low" | "normal" | "high";
}

export interface OrderIntent {
  side: OrderSide;
  order_type?: "market" | "limit" | "twap_sim";
  qty_or_notional: number;
  limit_price?: number;
  max_slippage_bps?: number;
  client_tag?: string;
  expire_ts?: number;
}

export interface StrategyContext {
  symbol: string;
  ts: number;
  account?: { equity: number; day_pnl: number };
  liquidity?: { spread_bps: number; adv_usd: number };
  position?: number;
  features?: Record<string, unknown>;
  meta?: Record<string, unknown>;
  book?: { bids: BookLevel[]; asks: BookLevel[] };
  tick?: { mid: number };
}

export interface RejectOut {
  tags: string[];
  notes: string;
}

export interface PaperOrderResult {
  fills: Fill[];
  reject?: RejectOut;
  trading_state?: TradingState;
}

export interface PipelineResult extends PaperOrderResult {
  signal?: SignalOut;
  risk: RiskOut;
  ctx?: {
    symbol: string;
    ts: number;
    tick?: { mid: number } | null;
    liquidity?: { spread_bps: number; adv_usd: number };
  };
}
