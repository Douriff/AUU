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
  auto_paper_orders?: boolean;
  strategy_autopaper?: boolean;
}

export type DataSource = "mock" | "paper" | "pumpfun_paper";

/** Market data provider (env DATA_PROVIDER). Orthogonal to order-path dataSource. */
export type MarketProvider = "mock" | "pumpfun_paper";

export interface PumpPaperParams {
  progress_bps_min: number;
  progress_bps_max: number;
  max_impact_bps: number;
  take_profit_pct: number;
  stop_loss_pct: number;
  max_hold_sec: number;
  cooldown_sec: number;
  max_day_loss_pct: number;
  max_open_mints: number;
  notional_pct_equity: number;
  auto_paper_orders: boolean;
  strategy_autopaper?: boolean;
  max_notional_sol: number;
}

export interface PumpPaperPosition {
  symbol: string;
  mint: string;
  qty: number;
  entry_price: number;
  entry_ts: number;
  entry_notional: number;
}

export interface PumpPaperState {
  strategyId: string;
  params: PumpPaperParams;
  auto_paper_orders: boolean;
  strategy_autopaper?: boolean;
  trading_state: TradingState;
  day_pnl?: number;
  positions: PumpPaperPosition[];
  last_decisions?: AutoDecision[];
  liveDisabled?: boolean;
}

export interface AutoDecision {
  ts: number;
  symbol: string;
  action: string;
  allow: boolean;
  reason: string;
  tags: string[];
  notes?: string;
}

export interface MonteCarloSim {
  n_paths: number;
  method?: string;
  seed?: number;
  sample_ok: boolean;
  note?: string;
  p05_pnl?: number;
  p50_pnl?: number;
  p95_pnl?: number;
  p05_dd?: number;
  p50_dd?: number;
  label?: string;
}

export interface RoundTrip {
  id: string;
  strategy_id: string;
  symbol: string;
  mint?: string | null;
  entry_ts: number;
  exit_ts: number;
  entry_price: number;
  exit_price: number;
  qty: number;
  pnl: number;
  pnl_pct: number;
  fees: number;
  tags: string[];
  source?: string;
  side?: string;
}

export interface EquityPoint {
  t: number;
  equity: number;
}

export interface PaperPerformance {
  mode: string;
  liveDisabled: boolean;
  window?: string | number;
  n_trades: number;
  trade_count?: number;
  wins: number;
  losses: number;
  win_rate: number | null;
  expectancy: number | null;
  max_drawdown_pct: number | null;
  sample_ok: boolean;
  mc?: boolean;
  open_lots?: number | null;
  fill_count?: number;
  monte_carlo: MonteCarloSim | null;
  equity: EquityPoint[];
  journal: RoundTrip[];
  equity_0?: number;
  disclaimer?: string;
  empty: boolean;
  auto_paper_orders?: boolean;
  strategy_autopaper?: boolean;
  strategyId?: string;
}

export interface MonitorRow {
  symbol: string;
  mint: string;
  kind?: string;
  progress_bps: number | null;
  phase: "curve" | "graduating" | "amm" | null;
  complete: boolean;
  migrated: boolean;
  price_sol: number | null;
  buy_notional_1m: number;
  sell_notional_1m: number;
  trade_count_1m: number;
  estimated_impact_bps: number | null;
  tags: string[];
  signal_side?: SignalSide | null;
  signal_reason?: string | null;
}

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

export interface NewTokenEvent {
  mint: string;
  creator: string;
  slot?: number | null;
  initial_reserves: Record<string, string>;
  ts: number;
  source: "pumpportal" | "logs";
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

export interface CurveSnapshot {
  symbol: string;
  mint?: string;
  venue?: string;
  quote?: string;
  virtual_sol_reserves?: string | number | null;
  virtual_token_reserves?: string | number | null;
  real_sol_reserves?: string | number | null;
  real_token_reserves?: string | number | null;
  curve_progress?: number | null;
  progress_bps?: number | null;
  graduated?: boolean;
  migrated?: boolean;
  complete?: boolean;
  price_sol?: number;
  phase?: string;
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
  liquidity?: {
    spread_bps: number;
    adv_usd: number;
    virtual_sol_reserves?: string;
    virtual_token_reserves?: string;
    real_sol_reserves?: string;
    real_token_reserves?: string;
    fee_bps?: number | null;
    protocol_fee_bps?: number | null;
    creator_fee_bps?: number | null;
  };
  position?: number;
  features?: Record<string, unknown>;
  meta?: Record<string, unknown>;
  book?: { bids: BookLevel[]; asks: BookLevel[] };
  tick?: { mid: number };
  pump?: PumpCtx | null;
}

export interface RejectOut {
  tags: string[];
  notes: string;
}

export type HabitTagName = "sniper" | "mid_curve" | "graduation_chase" | "flip" | "bag";
export type WatchSource = "portal" | "rpc" | "indexer";
export type TraderPhase = "curve" | "graduating" | "amm" | "unknown";

export interface TraderWatchlistItem {
  watch_id: string;
  address: string;
  label?: string | null;
  enabled: boolean;
  source: WatchSource;
  added_ts: number;
  tags_override: string[];
  risk_notes?: string | null;
}

export interface TraderPosition {
  mint: string;
  symbol?: string | null;
  qty: number;
  cost_basis_sol?: number | null;
  unrealized_pnl_sol?: number | null;
  hold_sec: number;
  progress_bps?: number | null;
  phase: TraderPhase;
}

export interface TradeBrief {
  ts: number;
  mint: string;
  side: "buy" | "sell";
  sol_amount: number;
  progress_bps?: number | null;
  signature?: string | null;
}

export interface TraderSnapshot {
  watch_id: string;
  address: string;
  asof_ts: number;
  slot?: number | null;
  positions: TraderPosition[];
  open_count: number;
  gross_exposure_sol: number;
  recent_buys: TradeBrief[];
  recent_sells: TradeBrief[];
  buy_notional_1h: number;
  sell_notional_1h: number;
  trade_count_1h: number;
  median_hold_sec_24h?: number | null;
  flip_rate_24h?: number | null;
  progress_hist: Record<string, number>;
  entry_progress_median_bps?: number | null;
}

export interface HabitTag {
  tag: HabitTagName;
  confidence: number;
  evidence: string[];
}

export interface HabitFeatures {
  median_entry_progress_bps?: number | null;
  pct_entries_lt_800: number;
  pct_entries_800_7500: number;
  pct_entries_gt_9000: number;
  median_hold_sec?: number | null;
  flip_rate_24h?: number | null;
  bag_score: number;
}

export interface HabitProfile {
  watch_id: string;
  address: string;
  asof_ts: number;
  tags: HabitTag[];
  primary: HabitTag | null;
  features: HabitFeatures;
}

export interface DistillResult {
  source_watch_id: string;
  asof_ts: number;
  suggested_params: Partial<PumpPaperParams>;
  feature_weights: { progress: number; momentum: number; impact: number };
  enabled_tags: string[];
  reject_reason?: string | null;
  paper_compare?: Record<string, unknown> | null;
  applied?: boolean;
  copy_trade_enabled?: boolean;
  note?: string;
}

export interface TraderWatchList {
  items: TraderWatchlistItem[];
  copy_trade_enabled: boolean;
  reader: string;
  helius_enabled?: boolean;
  liveDisabled: boolean;
}

export interface CompareReport {
  window: { from_ts?: number | null; to_ts?: number | null };
  self: PaperPerformance | Record<string, unknown>;
  trader_ref: {
    n_trades: number;
    approx_pnl: number;
    tags_hist: string[];
    reference_only?: boolean;
  };
  note: string;
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
    liquidity?: StrategyContext["liquidity"];
    pump?: PumpCtx | null;
  };
}
