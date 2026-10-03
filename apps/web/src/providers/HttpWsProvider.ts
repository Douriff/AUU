import type {
  MainstreamCandles,
  MainstreamFreshness,
  MainstreamFunding,
  MainstreamOverview,
  MainstreamTf,
  PaperAccount,
  StrategySummary,
  PaperOrder,
  PaperOrderRequest,
} from "@/types/mainstream";
import type {
  BookSnapshot,
  Candle,
  CurveSnapshot,
  Envelope,
  Fill,
  MonitorRow,
  NewTokenEvent,
  PaperOrderResult,
  PaperPerformance,
  AuthMe,
  AuthUser,
  BoardSnapshot,
  ConsoleFeed,
  Leaderboard,
  MajorsBoard,
  MajorsCompare,
  MajorsTickerBoard,
  MarketList,
  UniversePage,
  SearchCoinDetail,
  SearchResult,
  TradePosition,
  TradePreview,
  ExecutabilityReport,
  PostmortemReport,
  ShadowCompareReport,
  ExecReport,
  PipelineResult,
  PumpfunPaperSnapshot,
  PumpPaperParams,
  PumpPaperState,
  RejectEvent,
  RiskEvent,
  RiskOut,
  SignalOut,
  SymbolInfo,
  TradeTick,
  UserBook,
  WalletChallenge,
  WalletLedger,
  WalletPrepared,
  WalletPreparedOrder,
  WalletSignal,
  WalletStatus,
  TraderWatchList,
  TraderWatchlistItem,
  HabitProfile,
  DistillResult,
  CompareReport,
  TraderSnapshot,
  TradingStateEvent,
  LiveStatus,
  LiveLimits,
} from "@/types/contracts";

export type Channel = "candles" | "book" | "trades" | "signals" | "fills" | "risk";

export interface Handlers {
  onHello?: (msg: {
    version: number;
    providers: string[];
    orderMode?: string;
    eventTypes?: string[];
    venue?: string;
  }) => void;
  onCandle?: (c: Candle) => void;
  onBook?: (b: BookSnapshot) => void;
  onTrade?: (t: TradeTick) => void;
  onPumpfunCurve?: (s: PumpfunPaperSnapshot) => void;
  onSignal?: (s: SignalOut & { t: number; strategyId?: string; symbol?: string }) => void;
  onFill?: (f: Fill) => void;
  onRisk?: (r: RiskEvent) => void;
  onReject?: (r: RejectEvent) => void;
  onTradingState?: (t: TradingStateEvent) => void;
  onNewToken?: (t: NewTokenEvent) => void;
  onStatus?: (s: "connecting" | "open" | "closed" | "error") => void;
}

function apiBase(): string {
  const env = import.meta.env.VITE_API_BASE;
  if (env) return env.replace(/\/$/, "");
  return "";
}

function wsUrl(): string {
  const base = apiBase();
  if (base.startsWith("http")) {
    const u = new URL(base);
    u.protocol = u.protocol === "https:" ? "wss:" : "ws:";
    u.pathname = "/api/v1/ws";
    u.search = "";
    return u.toString();
  }
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${location.host}/api/v1/ws`;
}

/** Fired when the API answers 401 (session missing/expired) so the shell can show the login page. */
export const AUTH_REQUIRED_EVENT = "auu:auth-required";

function noteAuth(res: Response): void {
  if (res.status === 401 && typeof window !== "undefined") {
    window.dispatchEvent(new Event(AUTH_REQUIRED_EVENT));
  }
}

async function getJson<T>(path: string): Promise<T> {
  const res = await fetch(`${apiBase()}${path}`, { credentials: "include" });
  noteAuth(res);
  const body = (await res.json()) as Envelope<T>;
  if (!body.ok) {
    throw new Error(body.error?.message ?? "request failed");
  }
  return body.data;
}

async function sendJson<T>(path: string, body: unknown, method: "POST" | "PUT"): Promise<T> {
  const res = await fetch(`${apiBase()}${path}`, {
    method,
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  noteAuth(res);
  const env = (await res.json()) as Envelope<T>;
  if (!env.ok) {
    throw new Error(env.error?.message ?? "request failed");
  }
  return env.data;
}

async function postJson<T>(path: string, body: unknown): Promise<T> {
  return sendJson<T>(path, body, "POST");
}

async function putJson<T>(path: string, body: unknown): Promise<T> {
  return sendJson<T>(path, body, "PUT");
}

async function delJson<T>(path: string): Promise<T> {
  const res = await fetch(`${apiBase()}${path}`, { method: "DELETE", credentials: "include" });
  noteAuth(res);
  const env = (await res.json()) as Envelope<T>;
  if (!env.ok) {
    throw new Error(env.error?.message ?? "request failed");
  }
  return env.data;
}

export class HttpWsProvider {
  private ws: WebSocket | null = null;
  private handlers: Handlers = {};
  private pendingSubs: { channel: Channel; symbol: string; interval?: string }[] = [];
  private reconnectTimer: number | null = null;
  private intentionalClose = false;
  /** Bumps on each connect() so StrictMode unmount cannot reconnect a stale socket. */
  private epoch = 0;

  listSymbols(): Promise<SymbolInfo[]> {
    return getJson("/api/v1/symbols");
  }

  getCandles(symbol: string, interval = "1m"): Promise<Candle[]> {
    const q = new URLSearchParams({ symbol, interval });
    return getJson(`/api/v1/candles?${q}`);
  }

  getSignals(symbol: string): Promise<SignalOut[]> {
    const q = new URLSearchParams({ symbol });
    return getJson(`/api/v1/signals?${q}`);
  }

  getFills(symbol: string): Promise<Fill[]> {
    const q = new URLSearchParams({ symbol });
    return getJson(`/api/v1/fills?${q}`);
  }

  getHealth(): Promise<{
    status: string;
    provider: string;
    mode: string;
    venue?: string;
    quote?: string;
    defaultSymbol?: string;
    dataSourceOptions?: string[];
    marketProviderOptions?: string[];
    marketData?: "real" | "synthetic" | "mock" | string;
    marketDataLabel?: string;
    trading_state?: string;
    auto_paper_orders?: boolean;
    strategy_autopaper?: boolean;
    strategyId?: string;
    liveEnabled?: boolean;
    liveConfirmed?: boolean;
    liveDisabled?: boolean;
    liveArmed?: boolean;
    liveSendWired?: boolean;
    liveReasons?: string[];
    liveLimits?: LiveLimits;
    keypairConfigured?: boolean;
    keypairMounted?: boolean;
    pubkey?: string | null;
    keypairRelpath?: string;
    keypairEnv?: string;
    watch_mints?: string;
    discovery?: string;
    discoveryActive?: string;
    discoveryReason?: string;
    discoveryOptions?: string[];
    portal_key_configured?: boolean;
    copy_trade_enabled?: boolean;
    trader_watch_reader?: string;
    helius_enabled?: boolean;
    legacyPump?: boolean;
    mainstream?: MainstreamFreshness;
    runningStrategies?: string[];
  }> {
    return getJson("/api/v1/health");
  }

  getBook(symbol: string): Promise<BookSnapshot> {
    const q = new URLSearchParams({ symbol });
    return getJson(`/api/v1/book?${q}`);
  }

  getCurve(symbol: string): Promise<CurveSnapshot> {
    const q = new URLSearchParams({ symbol });
    return getJson(`/api/v1/curve?${q}`);
  }

  getPumpfunSnapshot(symbol: string): Promise<PumpfunPaperSnapshot> {
    const q = new URLSearchParams({ symbol });
    return getJson(`/api/v1/pumpfun/snapshot?${q}`);
  }

  getMonitor(): Promise<MonitorRow[]> {
    return getJson("/api/v1/pumpfun/monitor");
  }

  getStrategy(): Promise<PumpPaperState> {
    return getJson("/api/v1/strategy/pump-paper-v1");
  }

  getBoard(): Promise<BoardSnapshot> {
    return getJson("/api/v1/board");
  }

  getMarkets(): Promise<MarketList> {
    return getJson("/api/v1/markets");
  }

  getUniverse(q: {
    tab: string;
    offset?: number;
    limit?: number;
    q?: string;
    sort?: string;
    dir?: string;
    mints?: string;
  }): Promise<UniversePage> {
    const params = new URLSearchParams({ tab: q.tab });
    if (q.offset) params.set("offset", String(q.offset));
    if (q.limit) params.set("limit", String(q.limit));
    if (q.q) params.set("q", q.q);
    if (q.sort) params.set("sort", q.sort);
    if (q.dir) params.set("dir", q.dir);
    if (q.mints) params.set("mints", q.mints);
    return getJson(`/api/v1/universe?${params}`);
  }

  searchCoins(q: string, limit = 20): Promise<SearchResult> {
    const params = new URLSearchParams({ q, limit: String(limit) });
    return getJson(`/api/v1/search?${params}`);
  }

  getSearchCoin(mint: string): Promise<SearchCoinDetail> {
    const params = new URLSearchParams({ mint });
    return getJson(`/api/v1/search/coin?${params}`);
  }

  getMainstreamOverview(): Promise<MainstreamOverview> {
    return getJson("/api/v1/mainstream/overview");
  }

  getMainstreamCandles(symbol: string, tf: MainstreamTf, limit = 200, before?: number): Promise<MainstreamCandles> {
    const q = new URLSearchParams({ symbol, tf, limit: String(limit) });
    if (before != null) q.set("before", String(before));
    return getJson(`/api/v1/mainstream/candles?${q}`);
  }

  getPaperAccount(symbol?: string): Promise<PaperAccount> {
    const q = new URLSearchParams(symbol ? { symbol } : {});
    return getJson(`/api/v1/mainstream/paper/account?${q}`);
  }

  placePaperOrder(body: PaperOrderRequest): Promise<PaperOrder> {
    return postJson("/api/v1/mainstream/paper/orders", body);
  }

  cancelPaperOrder(id: string): Promise<PaperOrder> {
    return postJson(`/api/v1/mainstream/paper/orders/${encodeURIComponent(id)}/cancel`, {});
  }

  getStrategySummary(): Promise<StrategySummary> {
    return getJson("/api/v1/mainstream/strategy");
  }

  getMainstreamFunding(symbol: string, limit = 90): Promise<MainstreamFunding> {
    const q = new URLSearchParams({ symbol, limit: String(limit) });
    return getJson(`/api/v1/mainstream/funding?${q}`);
  }

  getMajors(): Promise<MajorsBoard> {
    return getJson("/api/v1/majors");
  }

  getMajorsTickers(q: {
    venue: string;
    limit?: number;
    q?: string;
    sort?: string;
    dir?: string;
    bucket?: string;
  }): Promise<MajorsTickerBoard> {
    const params = new URLSearchParams({ venue: q.venue });
    if (q.limit) params.set("limit", String(q.limit));
    if (q.q) params.set("q", q.q);
    if (q.sort) params.set("sort", q.sort);
    if (q.dir) params.set("dir", q.dir);
    if (q.bucket) params.set("bucket", q.bucket);
    return getJson(`/api/v1/majors/tickers?${params}`);
  }

  getMajorsCompare(base: string, onchainUsd?: number | null): Promise<MajorsCompare> {
    const params = new URLSearchParams({ base });
    if (onchainUsd != null && Number.isFinite(onchainUsd)) params.set("onchain_usd", String(onchainUsd));
    return getJson(`/api/v1/majors/compare?${params}`);
  }

  getTradePreview(q: {
    symbol?: string;
    mint?: string;
    side: "buy" | "sell";
    notional_sol?: number;
    sell_pct?: number;
  }): Promise<TradePreview> {
    const params = new URLSearchParams();
    if (q.symbol) params.set("symbol", q.symbol);
    if (q.mint) params.set("mint", q.mint);
    params.set("side", q.side);
    if (q.notional_sol != null) params.set("notional_sol", String(q.notional_sol));
    if (q.sell_pct != null) params.set("sell_pct", String(q.sell_pct));
    return getJson(`/api/v1/trade/preview?${params}`);
  }

  getTradePosition(q: { symbol?: string; mint?: string }): Promise<TradePosition> {
    const params = new URLSearchParams();
    if (q.symbol) params.set("symbol", q.symbol);
    if (q.mint) params.set("mint", q.mint);
    return getJson(`/api/v1/trade/position?${params}`);
  }

  getMe(): Promise<AuthMe> {
    return getJson("/api/v1/auth/me");
  }

  registerAccount(body: {
    name: string;
    password: string;
    password_confirm: string;
    display_name?: string;
    invite?: string;
    start_sol?: number;
    email?: string;
    email_code?: string;
  }): Promise<AuthMe> {
    return postJson("/api/v1/auth/register", body);
  }

  sendEmailCode(body: { email: string; purpose: "signup" | "reset" }): Promise<{
    sent: boolean;
    email: string;
    ttl_sec: number;
    resend_after_sec: number;
    message: string;
  }> {
    return postJson("/api/v1/auth/email/code", body);
  }

  resetPassword(body: { email: string; code: string; new_password: string; new_password_confirm: string }): Promise<{ ok: boolean; message: string }> {
    return postJson("/api/v1/auth/password/reset", body);
  }

  login(body: { name: string; password: string }): Promise<AuthMe> {
    return postJson("/api/v1/auth/login", body);
  }

  logout(): Promise<{ ok: boolean }> {
    return postJson("/api/v1/auth/logout", {});
  }

  changePassword(body: { current_password: string; new_password: string; new_password_confirm: string }): Promise<{ ok: boolean }> {
    return postJson("/api/v1/auth/password", body);
  }

  getWalletStatus(): Promise<WalletStatus> {
    return getJson("/api/v1/wallet/status");
  }

  walletChallenge(pubkey: string): Promise<WalletChallenge> {
    return postJson("/api/v1/wallet/challenge", { pubkey });
  }

  walletBind(body: { pubkey: string; nonce: string; signature: string }): Promise<WalletStatus> {
    return postJson("/api/v1/wallet/bind", body);
  }

  walletUnbind(): Promise<WalletStatus> {
    return postJson("/api/v1/wallet/unbind", {});
  }

  putWalletRisk(body: { max_notional_sol: number; max_day_loss_pct: number; max_open_positions: number }): Promise<WalletStatus> {
    return putJson("/api/v1/wallet/risk", body);
  }

  setWalletMode(body: { enabled: boolean; accept_risk: boolean }): Promise<WalletStatus> {
    return postJson("/api/v1/wallet/mode", body);
  }

  prepareWalletMemo(): Promise<WalletPrepared> {
    return postJson("/api/v1/wallet/devnet/prepare", {});
  }

  recordWalletTx(body: { prepare_id: string; signature: string }): Promise<{ liveEnabled: boolean; item: WalletLedger["items"][number] }> {
    return postJson("/api/v1/wallet/devnet/record", body);
  }

  getWalletLedger(): Promise<WalletLedger> {
    return getJson("/api/v1/wallet/ledger");
  }

  haltWallets(halt: boolean): Promise<{ global_halt: boolean; liveEnabled: boolean; mode: string }> {
    return postJson("/api/v1/wallet/admin/halt", { halt });
  }

  getWalletSignal(q: { mint?: string; price_sol?: number | null }): Promise<WalletSignal> {
    const params = new URLSearchParams();
    if (q.mint) params.set("mint", q.mint);
    if (q.price_sol != null && Number.isFinite(q.price_sol)) params.set("price_sol", String(q.price_sol));
    const suffix = params.toString();
    return getJson(`/api/v1/wallet/signal${suffix ? `?${suffix}` : ""}`);
  }

  prepareWalletOrder(body: {
    mint: string;
    side: "buy" | "sell";
    notional_sol?: number;
    sell_pct?: number;
    price_sol: number;
    slippage_bps?: number;
  }): Promise<WalletPreparedOrder> {
    return postJson("/api/v1/wallet/order/prepare", body);
  }

  submitWalletOrder(body: { prepare_id: string; signed_tx: string }): Promise<{ liveEnabled: boolean; real_money: boolean; item: WalletLedger["items"][number] }> {
    return postJson("/api/v1/wallet/order/submit", body);
  }

  getLeaderboard(sort: "pnl" | "return" = "pnl"): Promise<Leaderboard> {
    return getJson(`/api/v1/leaderboard?sort=${sort}`);
  }

  getAccounts(): Promise<{ items: AuthUser[] }> {
    return getJson("/api/v1/auth/users");
  }

  getTradeBook(userId?: string): Promise<UserBook> {
    const q = userId ? `?user_id=${encodeURIComponent(userId)}` : "";
    return getJson(`/api/v1/trade/book${q}`);
  }

  postTradeOrder(body: {
    symbol?: string;
    mint?: string;
    side: "buy" | "sell";
    notional_sol?: number;
    sell_pct?: number;
  }): Promise<TradePosition> {
    return postJson("/api/v1/trade/orders", body);
  }

  getEvents(since?: string, limit = 300): Promise<ConsoleFeed> {
    const q = new URLSearchParams();
    if (since) q.set("since", since);
    if (limit) q.set("limit", String(limit));
    const suffix = q.toString();
    return getJson(`/api/v1/events${suffix ? `?${suffix}` : ""}`);
  }

  getPaperPerformance(window = "session", opts?: { mc?: boolean }): Promise<PaperPerformance> {
    const q = new URLSearchParams({ window: String(window) });
    if (opts?.mc) q.set("mc", "1");
    return getJson(`/api/v1/strategy/pump-paper-v1/stats?${q}`);
  }

  getExecutability(window = "session"): Promise<ExecutabilityReport> {
    const q = new URLSearchParams({ window: String(window) });
    return getJson(`/api/v1/stats/executability?${q}`);
  }

  getPostmortem(opts?: {
    window?: string;
    n?: number;
    from?: number;
    to?: number;
    rolling?: number;
    scenario?: string;
  }): Promise<PostmortemReport> {
    const q = new URLSearchParams({
      window: String(opts?.window ?? "session"),
      n: String(opts?.n ?? 30),
      rolling: String(opts?.rolling ?? 10),
      scenario: String(opts?.scenario ?? "off"),
    });
    if (opts?.from != null) q.set("from", String(opts.from));
    if (opts?.to != null) q.set("to", String(opts.to));
    return getJson(`/api/v1/strategy/pump-paper-v1/postmortem?${q}`);
  }

  getShadowCompare(): Promise<ShadowCompareReport> {
    return getJson("/api/v1/strategy/pump-paper-v1/shadow-compare");
  }

  getExecReport(window = "session"): Promise<ExecReport> {
    const q = new URLSearchParams({ window: String(window) });
    return getJson(`/api/v1/stats/exec-report?${q}`);
  }

  getDecisionLog(fromTs?: number, toTs?: number) {
    const q = new URLSearchParams();
    if (fromTs != null) q.set("from", String(fromTs));
    if (toTs != null) q.set("to", String(toTs));
    const qs = q.toString();
    return getJson<{ items: Record<string, unknown>[]; n: number; liveEnabled: boolean }>(
      `/api/v1/strategy/pump-paper-v1/decision-log${qs ? `?${qs}` : ""}`
    );
  }

  putStrategy(patch: Partial<PumpPaperParams>) {
    return putJson<PumpPaperState>("/api/v1/strategy/pump-paper-v1", patch);
  }

  listWatchedTraders() {
    return getJson<TraderWatchList>("/api/v1/watch/traders");
  }

  putWatchedTrader(body: Partial<TraderWatchlistItem> & { address?: string }) {
    return putJson<TraderWatchList>("/api/v1/watch/traders", body);
  }

  deleteWatchedTrader(watchId: string) {
    return delJson<TraderWatchList>(`/api/v1/watch/traders/${encodeURIComponent(watchId)}`);
  }

  getTraderHabits(watchId: string) {
    return getJson<HabitProfile>(`/api/v1/watch/traders/${encodeURIComponent(watchId)}/habits`);
  }

  getTraderSnapshot(watchId: string) {
    return getJson<TraderSnapshot>(`/api/v1/watch/traders/${encodeURIComponent(watchId)}/snapshot`);
  }

  distillTrader(watchId: string) {
    return postJson<DistillResult>(`/api/v1/watch/traders/${encodeURIComponent(watchId)}/distill`, {});
  }

  applyDistill(body: {
    confirm: boolean;
    source_watch_id: string;
    suggested_params?: Partial<PumpPaperParams>;
  }) {
    return postJson<PumpPaperState & { distill?: Record<string, unknown> }>(
      "/api/v1/strategy/pump-paper-v1/apply-distill",
      body
    );
  }

  compareTrader(watchId: string) {
    return getJson<CompareReport>(`/api/v1/watch/traders/${encodeURIComponent(watchId)}/compare`);
  }

  preOrder(body: unknown) {
    return postJson<RiskOut>("/api/v1/risk/pre-order", body);
  }

  paperOrder(body: unknown) {
    return postJson<PaperOrderResult>("/api/v1/paper/orders", body);
  }

  postFill(body: unknown) {
    return postJson<{ trading_state?: string; tags: string[]; notes: string }>(
      "/api/v1/risk/post-fill",
      body
    );
  }

  decideAndFill(body: unknown) {
    return postJson<PipelineResult>("/api/v1/pipeline/decide-and-fill", body);
  }

  getLiveStatus(): Promise<LiveStatus> {
    return getJson("/api/v1/live/status");
  }

  putLiveLimits(body: Partial<LiveLimits>): Promise<LiveStatus> {
    return putJson<LiveStatus>("/api/v1/live/limits", body);
  }

  putLiveDisabled(live_disabled: boolean): Promise<LiveStatus> {
    return putJson<LiveStatus>("/api/v1/live/disabled", { live_disabled });
  }

  putLiveEnabled(liveEnabled: boolean, confirmed: boolean): Promise<LiveStatus> {
    return putJson<LiveStatus>("/api/v1/live/enabled", { liveEnabled, confirmed });
  }

  putLiveArm(armed: boolean): Promise<LiveStatus> {
    return putJson<LiveStatus>("/api/v1/live/arm", { armed });
  }

  connect(handlers: Handlers): () => void {
    this.handlers = handlers;
    this.intentionalClose = false;
    this.pendingSubs = [];
    const epoch = ++this.epoch;
    this.open(epoch);
    return () => {
      if (epoch !== this.epoch) return;
      this.intentionalClose = true;
      if (this.reconnectTimer) {
        window.clearTimeout(this.reconnectTimer);
        this.reconnectTimer = null;
      }
      const ws = this.ws;
      this.ws = null;
      if (ws) {
        ws.onclose = null;
        ws.close();
      }
    };
  }

  subscribe(channel: Channel, symbol: string, interval?: string) {
    const sub = { channel, symbol, interval };
    this.pendingSubs = this.pendingSubs.filter(
      (s) => !(s.channel === channel && s.symbol === symbol)
    );
    this.pendingSubs.push(sub);
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify({ type: "subscribe", ...sub }));
    }
  }

  unsubscribe(channel: Channel, symbol: string, interval?: string) {
    this.pendingSubs = this.pendingSubs.filter(
      (s) => !(s.channel === channel && s.symbol === symbol)
    );
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify({ type: "unsubscribe", channel, symbol, interval }));
    }
  }

  private open(epoch: number) {
    if (epoch !== this.epoch) return;
    this.handlers.onStatus?.("connecting");
    if (this.ws) {
      this.ws.onclose = null;
      this.ws.close();
      this.ws = null;
    }
    const ws = new WebSocket(wsUrl());
    this.ws = ws;

    ws.onopen = () => {
      if (epoch !== this.epoch) return;
      this.handlers.onStatus?.("open");
    };

    ws.onmessage = (ev) => {
      if (epoch !== this.epoch) return;
      let msg: Record<string, unknown>;
      try {
        msg = JSON.parse(String(ev.data));
      } catch {
        return;
      }
      const type = msg.type as string;
      if (type === "hello") {
        this.handlers.onHello?.({
          version: msg.version as number,
          providers: (msg.providers as string[]) ?? [],
          orderMode: msg.orderMode as string | undefined,
          eventTypes: msg.eventTypes as string[] | undefined,
          venue: msg.venue as string | undefined,
        });
        for (const s of this.pendingSubs) {
          ws.send(JSON.stringify({ type: "subscribe", ...s }));
        }
        return;
      }
      if (type === "ping") {
        ws.send(JSON.stringify({ type: "pong" }));
        return;
      }
      if (type === "candle") this.handlers.onCandle?.(msg.payload as Candle);
      if (type === "book") this.handlers.onBook?.(msg.payload as BookSnapshot);
      if (type === "trade") this.handlers.onTrade?.(msg.payload as TradeTick);
      if (type === "pumpfun_curve")
        this.handlers.onPumpfunCurve?.(msg.payload as PumpfunPaperSnapshot);
      if (type === "signal") {
        const p = msg.payload as {
          strategyId: string;
          symbol: string;
          t: number;
          signal: SignalOut;
        };
        this.handlers.onSignal?.({
          ...p.signal,
          t: p.t,
          strategyId: p.strategyId,
          symbol: p.symbol,
        });
      }
      if (type === "fill") this.handlers.onFill?.(msg.payload as Fill);
      if (type === "risk") this.handlers.onRisk?.(msg.payload as RiskEvent);
      if (type === "reject") this.handlers.onReject?.(msg.payload as RejectEvent);
      if (type === "trading_state")
        this.handlers.onTradingState?.(msg.payload as TradingStateEvent);
      if (type === "new_token") this.handlers.onNewToken?.(msg.payload as NewTokenEvent);
    };

    ws.onerror = () => {
      if (epoch !== this.epoch) return;
      this.handlers.onStatus?.("error");
    };
    ws.onclose = () => {
      if (epoch !== this.epoch) return;
      this.handlers.onStatus?.("closed");
      if (!this.intentionalClose) {
        this.reconnectTimer = window.setTimeout(() => this.open(epoch), 2000);
      }
    };
  }
}

export const marketProvider = new HttpWsProvider();
