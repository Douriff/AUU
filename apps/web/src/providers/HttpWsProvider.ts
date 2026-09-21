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

async function getJson<T>(path: string): Promise<T> {
  const res = await fetch(`${apiBase()}${path}`);
  const body = (await res.json()) as Envelope<T>;
  if (!body.ok) {
    throw new Error(body.error?.message ?? "request failed");
  }
  return body.data;
}

async function sendJson<T>(path: string, body: unknown, method: "POST" | "PUT"): Promise<T> {
  const res = await fetch(`${apiBase()}${path}`, {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
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
    pubkeyShort?: string | null;
    keypairRelpath?: string;
    keypairEnv?: string;
    watch_mints?: string;
    discovery?: string;
    discoveryOptions?: string[];
    portal_key_configured?: boolean;
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

  getPaperPerformance(window = "session", opts?: { mc?: boolean }): Promise<PaperPerformance> {
    const q = new URLSearchParams({ window: String(window) });
    if (opts?.mc) q.set("mc", "1");
    return getJson(`/api/v1/strategy/pump-paper-v1/stats?${q}`);
  }

  putStrategy(patch: Partial<PumpPaperParams>) {
    return putJson<PumpPaperState>("/api/v1/strategy/pump-paper-v1", patch);
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
