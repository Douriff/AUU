import type {
  BookSnapshot,
  Candle,
  Envelope,
  Fill,
  RiskEvent,
  SignalOut,
  SymbolInfo,
  TradeTick,
} from "@/types/contracts";

export type Channel = "candles" | "book" | "trades" | "signals" | "fills" | "risk";

export interface Handlers {
  onHello?: (msg: { version: number; providers: string[] }) => void;
  onCandle?: (c: Candle) => void;
  onBook?: (b: BookSnapshot) => void;
  onTrade?: (t: TradeTick) => void;
  onSignal?: (s: SignalOut & { t: number; strategyId?: string; symbol?: string }) => void;
  onFill?: (f: Fill) => void;
  onRisk?: (r: RiskEvent) => void;
  onStatus?: (s: "connecting" | "open" | "closed" | "error") => void;
}

function apiBase(): string {
  const env = import.meta.env.VITE_API_BASE;
  if (env) return env.replace(/\/$/, "");
  // same-origin / vite proxy
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

export class HttpWsProvider {
  private ws: WebSocket | null = null;
  private handlers: Handlers = {};
  private pendingSubs: { channel: Channel; symbol: string; interval?: string }[] = [];
  private reconnectTimer: number | null = null;
  private intentionalClose = false;

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

  getHealth(): Promise<{ status: string; provider: string; mode: string }> {
    return getJson("/api/v1/health");
  }

  connect(handlers: Handlers): () => void {
    this.handlers = handlers;
    this.intentionalClose = false;
    this.open();
    return () => {
      this.intentionalClose = true;
      if (this.reconnectTimer) window.clearTimeout(this.reconnectTimer);
      this.ws?.close();
      this.ws = null;
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

  private open() {
    this.handlers.onStatus?.("connecting");
    const ws = new WebSocket(wsUrl());
    this.ws = ws;

    ws.onopen = () => {
      this.handlers.onStatus?.("open");
    };

    ws.onmessage = (ev) => {
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
    };

    ws.onerror = () => this.handlers.onStatus?.("error");
    ws.onclose = () => {
      this.handlers.onStatus?.("closed");
      if (!this.intentionalClose) {
        this.reconnectTimer = window.setTimeout(() => this.open(), 2000);
      }
    };
  }
}

export const marketProvider = new HttpWsProvider();
