export type ReviewStatus = "ok" | "empty" | "not_connected" | "non_finite" | "inconsistent" | "unreadable" | string;

export interface ReviewPerformance {
  status: ReviewStatus;
  reason: string | null;
  book: string;
  basis: string | null;
  principal: number | null;
  principal_ccy: string | null;
  total_return: number | null;
  max_drawdown: number | null;
  cost: number | null;
  cost_status: string;
  cost_unit?: string | null;
  closed_trades: number | null;
  open_trades: number | null;
  nav_points: number | null;
  signal_rows: number | null;
  as_of_ts: number | null;
  recorded_at: number | null;
  stale: boolean | null;
  warnings: string[];
  curve: { ts: number; nav: number }[];
  window: { start_ts: number; end_ts: number; tf_ms: number; n: number } | null;
  method: string | null;
}

export interface ReviewStrategy {
  id: string;
  version: string;
  version_kind: string;
  style: string;
  direction: string;
  timeframe: string;
  pool: { symbols: string[] | null; size: number | null; source: string; coverage: string };
  hold: { kind: string; enter: number | null; exit: number | null; hours?: number; fill?: string };
  primary_book: string;
  registered_at: string | null;
  records: string;
  orders: boolean;
  performance: ReviewPerformance;
  backtest: ReviewPerformance;
  trade_stats: { not_a_nav?: boolean; closed_trades?: number; cost?: number | null; cost_status?: string } | null;
  data_origin: string;
}

export interface ReviewCondition {
  id: string;
  status: "met" | "unmet" | "unknown" | "not_in_rule" | "not_applicable" | string;
  observed?: number | string | null;
  threshold?: number;
  op?: string;
  source?: string;
  recorded?: boolean;
  expected?: number | string | null;
}

export interface ReviewRules {
  label: string;
  ai_generated: boolean;
  reconstructed: string;
  strategy_id: string;
  symbol: string | null;
  asof_ts: number | null;
  recorded_at: number | null;
  side: string | null;
  score: number | null;
  action: string | null;
  reason: string | null;
  conditions: ReviewCondition[];
  invalidation: { id: string; status: string; threshold?: number }[];
  risks: { id: string }[];
}

export interface ReviewAi {
  status: string;
  live: boolean;
  text: string | null;
  reason?: string | null;
  mock?: boolean;
}

export interface ReviewEvent {
  id: string;
  ts: number | null;
  book: string;
  strategy_id: string;
  kind: string;
  symbol: string | null;
  side: string | null;
  price: number | null;
  cost: number | null;
  gross: number | null;
  net: number | null;
  unrealized: number | null;
  basis: string;
  live_order: boolean;
  record_class: string;
  open?: boolean;
}

export interface ReviewPayload {
  mode: string;
  sample: boolean;
  fixture_id: string | null;
  fixture_error: string | null;
  notice: { profit_promise: boolean; orders_enabled: boolean; live_enabled: boolean; read_only: boolean };
  timing: { late_entry: null; early_exit: null; status: string; reason: string };
  ai: ReviewAi;
  strategies: ReviewStrategy[];
  compare: {
    groups: { ids: string[]; reason: string; window: ReviewPerformance["window"]; rows: { id: string; total_return: number | null; max_drawdown: number | null; cost: number | null; cost_status: string; closed_trades: number | null; nav_points: number | null; warnings: string[] }[] }[];
    refused: { ids: string[]; reason: string }[];
  };
  signals: { strategy_id: string; symbol: string; asof_ts: number; side: string; score: number; action: string; recorded_at: number | null }[];
  featured: { rules: ReviewRules; ai: ReviewAi; sample?: boolean } | null;
  decisions: ReviewEvent[];
  live: { status: string; reason: string };
  generated_at: number;
  sources: { name: string; state: string }[];
}

export class ReviewHttpError extends Error {
  constructor(readonly status: number, readonly code: string) {
    super(code);
  }
}

async function read(path: string, signal?: AbortSignal): Promise<ReviewPayload> {
  const res = await fetch(path, { credentials: "include", signal, headers: { Accept: "application/json" } });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    const code = body?.error?.code || "ERROR";
    throw new ReviewHttpError(res.status, code);
  }
  return body.data as ReviewPayload;
}

export function loadPlaza(signal?: AbortSignal): Promise<ReviewPayload> {
  return read("/api/v1/ai-review/plaza", signal);
}

export function loadExplain(strategy: string, symbol: string, asof: number, signal?: AbortSignal): Promise<{ rules: ReviewRules; ai: ReviewAi }> {
  const q = new URLSearchParams({ strategy, symbol, asof: String(asof) });
  return fetch(`/api/v1/ai-review/signals/explain?${q}`, { credentials: "include", signal }).then(async (res) => {
    const body = await res.json().catch(() => ({}));
    if (!res.ok) throw new ReviewHttpError(res.status, body?.error?.code || "ERROR");
    return body.data;
  });
}

export async function downloadCsv(): Promise<void> {
  const res = await fetch("/api/v1/ai-review/plaza.csv", { credentials: "include" });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new ReviewHttpError(res.status, body?.error?.code || "ERROR");
  }
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = "strategy-review.csv";
  link.click();
  URL.revokeObjectURL(url);
}
