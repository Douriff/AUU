/** One market-data WebSocket to /api/v1/ws: subscribe diffing, liveness, reconnect with backoff + jitter.
 *
 *  Live = the socket is open, the hello advertised ``realtime``, the last ``rt_status`` said live, the stream's
 *  source is the exchange the page's REST data comes from, and a ticker/kline frame arrived within
 *  ``DATA_GAP_MS``. Anything else is reported as connecting/polling and the pages keep their own REST polling.
 *  A hello without ``realtime`` (backend switch off / push off) marks the feature unsupported: the socket is
 *  closed and never reopened by this instance. */
import { backoffDelay } from "./backoff";
import { keyOf, specOf, type RtEnv, type RtState, type SubSpec, type WsLike } from "./types";

const OPEN = 1;
/** No market frame for this long while subscribed -> not live (plan: "5 秒没收到任何行情帧"). */
export const DATA_GAP_MS = 5_000;
/** A connection that delivered data for this long resets the backoff. */
export const STABLE_MS = 10_000;
// Client-side send budget, under the server's 20 messages / 10 s token bucket (pongs included there).
const SEND_BURST = 12;
const SEND_RATE_PER_S = 1.5;

export interface ConnStatus {
  state: RtState;
  source: string | null;
  reason: string | null;
}

export interface ConnOpts {
  env: RtEnv;
  expectedEx: () => string | null;
  onFrame: (kind: "ticker" | "kline", payload: Record<string, unknown>) => void;
  onStatus: (s: ConnStatus) => void;
}

export class RtConnection {
  private ws: WsLike | null = null;
  private running = false;
  private attempt = 0;
  private timer: unknown = null;
  private watch: unknown = null;
  private flushTimer: unknown = null;
  private ready = false;
  private rtLive = false;
  private source: string | null = null;
  private lastDataAt = 0;
  private openedAt = 0;
  private gotData = false;
  private sent = new Set<string>();
  private want = new Set<string>();
  private queue: string[] = [];
  private tokens = SEND_BURST;
  private tokAt = 0;
  unsupported = false;
  connects = 0;
  delays: number[] = [];
  status: ConnStatus = { state: "connecting", source: null, reason: "connecting" };

  constructor(private o: ConnOpts) {}

  get isRunning() {
    return this.running;
  }

  setKeys(keys: Iterable<string>) {
    this.want = new Set(keys);
    if (this.ready) this.sync();
    this.evaluate();
  }

  start() {
    if (this.running || this.unsupported) return;
    this.running = true;
    this.attempt = 0;
    this.open();
    this.tickWatch();
  }

  /** Close and stay closed (page hidden, feature disabled, leadership handed over). */
  stop(reason = "hidden") {
    this.running = false;
    for (const t of [this.timer, this.watch, this.flushTimer]) if (t != null) this.o.env.clearTimeout(t);
    this.timer = this.watch = this.flushTimer = null;
    const ws = this.ws;
    this.ws = null;
    if (ws) {
      ws.onclose = ws.onmessage = ws.onerror = ws.onopen = null;
      try {
        ws.close(1000, reason);
      } catch {
        /* already closed */
      }
    }
    this.reset();
    this.set(this.unsupported ? { state: "off", source: null, reason: "no-push" } : { state: "polling", source: this.source, reason });
  }

  /** Allow a new attempt after the backend said "no push" (e.g. the switch was turned on later). */
  rearm() {
    this.unsupported = false;
  }

  private reset() {
    this.ready = false;
    this.sent.clear();
    this.queue = [];
    this.rtLive = false;
    this.lastDataAt = 0;
  }

  private tickWatch() {
    this.watch = this.o.env.setTimeout(() => {
      this.watch = null;
      if (!this.running) return;
      this.evaluate();
      this.tickWatch();
    }, 1000);
  }

  private open() {
    const env = this.o.env;
    this.connects += 1;
    this.reset();
    this.openedAt = env.now();
    this.gotData = false;
    let ws: WsLike;
    try {
      ws = env.makeWs(env.url());
    } catch {
      this.scheduleReconnect();
      return;
    }
    this.ws = ws;
    ws.onopen = () => this.evaluate();
    ws.onmessage = (ev) => {
      if (this.ws === ws) this.onMessage(ws, ev.data);
    };
    ws.onerror = () => undefined; // a close event follows
    ws.onclose = () => {
      if (this.ws !== ws) return;
      this.ws = null;
      this.reset();
      if (!this.running || this.unsupported) return this.evaluate();
      if (this.gotData && env.now() - this.openedAt >= STABLE_MS) this.attempt = 0;
      this.scheduleReconnect();
    };
    this.evaluate();
  }

  private scheduleReconnect() {
    this.attempt += 1;
    const delay = backoffDelay(this.attempt, this.o.env.rand);
    this.delays.push(delay);
    if (this.delays.length > 50) this.delays.splice(0, this.delays.length - 50);
    this.timer = this.o.env.setTimeout(() => {
      this.timer = null;
      if (this.running) this.open();
    }, delay);
    this.set({ state: "polling", source: this.source, reason: "closed" });
  }

  private onMessage(ws: WsLike, raw: unknown) {
    let msg: Record<string, unknown>;
    try {
      msg = JSON.parse(String(raw));
    } catch {
      return;
    }
    const type = msg.type;
    if (type === "hello") {
      if (!msg.realtime) {
        // switch off (or push off): never stream, the page keeps its exact old behaviour
        this.unsupported = true;
        this.stop("no-push");
        return;
      }
      this.ready = true;
      this.sync();
      return this.evaluate();
    }
    if (type === "ping") {
      try {
        ws.send(JSON.stringify({ type: "pong" }));
      } catch {
        /* closing */
      }
      return;
    }
    if (type === "rt_status") {
      const p = (msg.payload ?? {}) as { live?: boolean; source?: string | null };
      this.rtLive = Boolean(p.live);
      this.source = p.source ?? null;
      return this.evaluate();
    }
    if (type === "ticker" || type === "kline") {
      this.lastDataAt = this.o.env.now();
      this.gotData = true;
      this.o.onFrame(type, (msg.payload ?? {}) as Record<string, unknown>);
      if (this.status.state !== "live") this.evaluate();
    }
  }

  private sync() {
    const add = [...this.want].filter((k) => !this.sent.has(k)).map(specOf);
    const del = [...this.sent].filter((k) => !this.want.has(k)).map(specOf);
    const msgs: string[] = [];
    const group = (type: "subscribe" | "unsubscribe", specs: SubSpec[]) => {
      const tick = specs.filter((s) => s.channel === "ticker").map((s) => s.symbol);
      if (tick.length) msgs.push(JSON.stringify({ type, channel: "ticker", symbols: tick }));
      for (const s of specs) if (s.channel === "kline") msgs.push(JSON.stringify({ type, channel: "kline", symbol: s.symbol, interval: s.interval }));
    };
    group("unsubscribe", del);
    group("subscribe", add);
    for (const s of del) this.sent.delete(keyOf(s));
    for (const s of add) this.sent.add(keyOf(s));
    this.queue.push(...msgs);
    this.flush();
  }

  private flush() {
    const env = this.o.env;
    const now = env.now();
    this.tokens = Math.min(SEND_BURST, this.tokens + ((now - this.tokAt) / 1000) * SEND_RATE_PER_S);
    this.tokAt = now;
    while (this.queue.length && this.tokens >= 1 && this.ws && this.ws.readyState === OPEN) {
      const m = this.queue.shift()!;
      try {
        this.ws.send(m);
      } catch {
        break;
      }
      this.tokens -= 1;
    }
    if (this.queue.length && this.flushTimer == null && this.running) {
      const wait = Math.ceil(((1 - this.tokens) / SEND_RATE_PER_S) * 1000) + 5;
      this.flushTimer = env.setTimeout(() => {
        this.flushTimer = null;
        this.flush();
      }, Math.max(50, wait));
    }
  }

  private evaluate() {
    const now = this.o.env.now();
    let next: ConnStatus;
    if (this.unsupported) next = { state: "off", source: null, reason: "no-push" };
    else if (!this.running) next = { state: "polling", source: this.source, reason: this.status.reason ?? "stopped" };
    else if (!this.ws || this.ws.readyState !== OPEN || !this.ready)
      next = { state: this.attempt > 0 ? "polling" : "connecting", source: this.source, reason: this.attempt > 0 ? "closed" : "connecting" };
    else if (!this.rtLive) next = { state: "polling", source: this.source, reason: "rt-down" };
    else {
      const exp = this.o.expectedEx();
      if (exp && this.source && this.source !== exp) next = { state: "polling", source: this.source, reason: "source" };
      else if (!this.lastDataAt) next = { state: "connecting", source: this.source, reason: "waiting" };
      else if (now - this.lastDataAt > DATA_GAP_MS) next = { state: "polling", source: this.source, reason: "stale" };
      else next = { state: "live", source: this.source, reason: null };
    }
    this.set(next);
  }

  private set(next: ConnStatus) {
    const s = this.status;
    if (s.state === next.state && s.source === next.source && s.reason === next.reason) return;
    this.status = next;
    this.o.onStatus(next);
  }
}
