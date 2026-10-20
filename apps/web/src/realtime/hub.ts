/** Per-tab entry point for realtime market data (docs/realtime-ws-plan.md step 3).
 *
 *  Pages call :meth:`RtHub.configure` with the ``realtime`` block of ``GET /api/v1/mainstream/markets``; that block
 *  only exists while the backend runs the feed with push on. Without it the hub stays ``off`` and never opens a
 *  socket, so the pages behave exactly as before (same requests, same polling).
 *
 *  Transport:
 *  - shared (BroadcastChannel + Web Locks available): the tab holding the ``auu-rt-leader`` lock owns the one
 *    WebSocket, subscribes the union of every tab's keys and relays frames + status on the channel; other tabs
 *    are followers. Only visible tabs with subscriptions compete for the lock; a leader hidden for
 *    ``HIDDEN_GRACE_MS`` hands over (closing its socket), so with every tab hidden there is no connection at all;
 *  - solo (either API missing): one connection per tab, same rules.
 *
 *  Pages read :meth:`getStatus` (``live`` -> slow their polling down; anything else -> normal polling). */
import { RtConnection, DATA_GAP_MS, type ConnStatus } from "./connection";
import { keyOf, type BcLike, type RtCaps, type RtEnv, type RtKline, type RtStatus, type RtTicker } from "./types";

export const HIDDEN_GRACE_MS = 15_000;
/** Keep the socket this long after the last subscription went away (a coin / page switch unsubscribes and
 *  re-subscribes within one React commit; this avoids a close + reconnect each time). */
export const IDLE_GRACE_MS = 5_000;
const CHANNEL = "auu-rt-v1";
const LOCK = "auu-rt-leader";
const TICK_MS = 1_000;
const PEER_TTL_MS = 6_000;
const LEADER_TTL_MS = 5_000;

let tabSeq = 0;
const OFF: RtStatus = { state: "off", source: null, reason: null, shared: false, leader: false };

type Cb = (p: never) => void;
type Msg =
  | { t: "subs"; tab: string; keys: string[] }
  | { t: "status"; tab: string; st: ConnStatus }
  | { t: "frame"; tab: string; kind: "ticker" | "kline"; p: Record<string, unknown> }
  | { t: "lead"; tab: string }
  | { t: "bye"; tab: string };

export class RtHub {
  private caps: RtCaps | null = null;
  private expectedEx: string | null = null;
  private enabled = false;
  private listeners = new Map<string, Set<Cb>>();
  private status: RtStatus = OFF;
  private statusSubs = new Set<() => void>();
  private conn: RtConnection | null = null;
  readonly shared: boolean;
  readonly tab: string;
  private bc: BcLike | null = null;
  private peers = new Map<string, { keys: string[]; at: number }>();
  private leader = false;
  private queued = false;
  private lockAbort: AbortController | null = null;
  private releaseLock: (() => void) | null = null;
  private leaderTab: string | null = null;
  private leaderStatus: ConnStatus | null = null;
  private leaderSeenAt = 0;
  private lastFrameAt = 0;
  private followSince = 0;
  private hiddenSince: number | null = null;
  private hiddenTimer: unknown = null;
  private tickTimer: unknown = null;
  private ticks = 0;
  private lastSentKeys = "";
  private offVis: (() => void) | null = null;
  private offHide: (() => void) | null = null;
  private unsupported = false;
  private unsupportedAt = 0;
  private keysAt = 0;
  private idleTimer: unknown = null;

  constructor(private env: RtEnv, private disabledByBuild = false) {
    this.shared = Boolean(env.makeChannel && env.locks);
    this.tab = `${env.now().toString(36)}-${Math.floor(Math.random() * 1e9).toString(36)}-${++tabSeq}`;
  }

  // ---- public API -----------------------------------------------------------------------------
  /** ``caps`` = ``markets.realtime`` (null/undefined when the backend does not push); ``expectedEx`` = the
   *  exchange the page's REST data comes from (frames from another venue are not applied). */
  configure(caps: RtCaps | null | undefined, expectedEx: string | null | undefined) {
    if (!caps || !caps.push || this.disabledByBuild) return this.disable();
    this.caps = caps;
    this.expectedEx = expectedEx ?? null;
    if (this.unsupported && this.env.now() - this.unsupportedAt > 60_000) this.unsupported = false;
    if (this.unsupported) return;
    if (!this.enabled) this.setup();
    this.update();
  }

  disable() {
    if (!this.enabled) return;
    this.enabled = false;
    this.stepDown("disabled");
    this.abortLock();
    this.conn?.stop("disabled");
    this.conn = null;
    for (const t of [this.hiddenTimer, this.tickTimer, this.idleTimer]) if (t != null) this.env.clearTimeout(t);
    this.hiddenTimer = this.tickTimer = this.idleTimer = null;
    this.post({ t: "bye", tab: this.tab });
    this.bc?.close();
    this.bc = null;
    this.offVis?.();
    this.offHide?.();
    this.offVis = this.offHide = null;
    this.peers.clear();
    this.leaderStatus = null;
    this.setStatus(OFF);
  }

  supports(tf: string) {
    return Boolean(this.enabled && this.caps?.intervals.includes(tf));
  }

  isLive() {
    return this.status.state === "live";
  }

  getStatus = (): RtStatus => this.status;

  onStatus = (cb: () => void) => {
    this.statusSubs.add(cb);
    return () => void this.statusSubs.delete(cb);
  };

  subscribeTicker(symbols: string[], cb: (t: RtTicker) => void): () => void {
    const offs = symbols.map((s) => this.listen(keyOf({ channel: "ticker", symbol: s }), cb as Cb));
    return () => offs.forEach((f) => f());
  }

  subscribeKline(symbol: string, tf: string, cb: (k: RtKline) => void): () => void {
    return this.listen(keyOf({ channel: "kline", symbol, interval: tf }), cb as Cb);
  }

  /** test / status helpers */
  get connection() {
    return this.conn;
  }
  get isLeader() {
    return this.leader;
  }

  // ---- listeners / keys -------------------------------------------------------------------------
  private listen(key: string, cb: Cb) {
    let set = this.listeners.get(key);
    if (!set) this.listeners.set(key, (set = new Set()));
    set.add(cb);
    this.update();
    return () => {
      const s = this.listeners.get(key);
      if (!s) return;
      s.delete(cb);
      if (!s.size) this.listeners.delete(key);
      this.update();
    };
  }

  private localKeys(): string[] {
    const caps = this.caps;
    if (!caps) return [];
    const syms = new Set(caps.symbols);
    const tfs = new Set(caps.intervals);
    return [...this.listeners.keys()].filter((k) => {
      const [ch, sym, tf] = k.split(":");
      return syms.has(sym) && (ch === "ticker" || tfs.has(tf));
    }).sort();
  }

  private hiddenLong() {
    return this.hiddenSince != null && this.env.now() - this.hiddenSince >= HIDDEN_GRACE_MS;
  }

  private wantLead() {
    if (!this.enabled || this.hiddenLong()) return false;
    if (this.localKeys().length > 0) return true;
    return this.keysAt > 0 && this.env.now() - this.keysAt < IDLE_GRACE_MS;
  }

  private noteKeys() {
    const has = this.localKeys().length > 0;
    if (has) {
      this.keysAt = this.env.now();
      return;
    }
    if (this.keysAt > 0 && this.idleTimer == null && this.enabled) {
      this.keysAt = this.env.now();
      this.idleTimer = this.env.setTimeout(() => {
        this.idleTimer = null;
        this.update();
      }, IDLE_GRACE_MS + 1);
    }
  }

  private unionKeys(): string[] {
    const all = new Set(this.localKeys());
    const now = this.env.now();
    for (const p of this.peers.values()) if (now - p.at <= PEER_TTL_MS) p.keys.forEach((k) => all.add(k));
    return [...all];
  }

  // ---- lifecycle ----------------------------------------------------------------------------------
  private setup() {
    this.enabled = true;
    const env = this.env;
    this.hiddenSince = env.isHidden() ? env.now() : null;
    this.offVis = env.onVisibility(() => this.onVisibility());
    this.offHide = env.onPageHide?.(() => this.onPageHide()) ?? null;
    if (this.shared && env.makeChannel) {
      this.bc = env.makeChannel(CHANNEL);
      this.bc.onmessage = (ev) => this.onChannel(ev.data as Msg);
    }
    this.tick();
  }

  private tick() {
    this.tickTimer = this.env.setTimeout(() => {
      this.tickTimer = null;
      if (!this.enabled) return;
      this.ticks += 1;
      const now = this.env.now();
      if (this.shared) {
        let pruned = false;
        for (const [id, p] of this.peers) if (now - p.at > PEER_TTL_MS) (this.peers.delete(id), (pruned = true));
        if (this.ticks % 2 === 0) {
          this.post({ t: "subs", tab: this.tab, keys: this.wantLead() ? this.localKeys() : [] });
          if (this.leader && this.conn) this.post({ t: "status", tab: this.tab, st: this.conn.status });
        }
        if (pruned && this.leader) this.conn?.setKeys(this.unionKeys());
      }
      this.recompute();
      this.tick();
    }, TICK_MS);
  }

  private onVisibility() {
    const env = this.env;
    if (env.isHidden()) {
      if (this.hiddenSince == null) this.hiddenSince = env.now();
      if (this.hiddenTimer != null) env.clearTimeout(this.hiddenTimer);
      this.hiddenTimer = env.setTimeout(() => {
        this.hiddenTimer = null;
        this.update();
      }, HIDDEN_GRACE_MS);
    } else {
      this.hiddenSince = null;
      if (this.hiddenTimer != null) env.clearTimeout(this.hiddenTimer);
      this.hiddenTimer = null;
      this.update();
    }
  }

  /** Tab closing (or entering the back/forward cache): release everything at once so another tab can lead;
   *  a restored page re-enables on its next markets poll. */
  private onPageHide() {
    this.disable();
  }

  /** Reconcile connection / leadership with the current keys, visibility and peers. */
  private update() {
    if (!this.enabled) return;
    if (this.localKeys().length > 0 && this.idleTimer != null) {
      this.env.clearTimeout(this.idleTimer);
      this.idleTimer = null;
    }
    this.noteKeys();
    const want = this.wantLead();
    if (!this.shared) {
      if (want) {
        if (!this.conn) this.conn = this.makeConn();
        this.conn.setKeys(this.localKeys());
        if (!this.conn.isRunning) this.conn.start();
      } else if (this.conn?.isRunning) {
        this.conn.stop(this.hiddenLong() ? "hidden" : "idle");
      }
      return this.recompute();
    }
    const keys = want ? this.localKeys() : [];
    const sig = keys.join(",");
    if (sig !== this.lastSentKeys) {
      this.lastSentKeys = sig;
      if (keys.length) this.followSince = this.env.now();
      this.post({ t: "subs", tab: this.tab, keys });
    }
    if (this.leader) {
      if (!want) this.stepDown(this.hiddenLong() ? "hidden" : "idle");
      else this.conn?.setKeys(this.unionKeys());
    } else if (want && !this.queued) this.requestLock();
    else if (!want && this.queued) this.abortLock();
    this.recompute();
  }

  private makeConn() {
    return new RtConnection({
      env: this.env,
      expectedEx: () => this.expectedEx,
      onFrame: (kind, p) => {
        this.dispatch(kind, p);
        if (this.leader) this.post({ t: "frame", tab: this.tab, kind, p });
      },
      onStatus: (st) => {
        if (this.leader) this.post({ t: "status", tab: this.tab, st });
        if (this.conn?.unsupported) {
          this.unsupported = true;
          this.unsupportedAt = this.env.now();
        }
        this.recompute();
      },
    });
  }

  // ---- leadership (shared mode) -----------------------------------------------------------------
  private requestLock() {
    const locks = this.env.locks;
    if (!locks) return;
    this.queued = true;
    const ac = typeof AbortController !== "undefined" ? new AbortController() : null;
    this.lockAbort = ac;
    locks
      .request(LOCK, ac ? { signal: ac.signal } : {}, () =>
        new Promise<void>((resolve) => {
          this.queued = false;
          this.lockAbort = null;
          if (!this.wantLead()) return resolve(); // granted after we stopped needing it
          this.releaseLock = resolve;
          this.becomeLeader();
        })
      )
      .catch(() => {
        this.queued = false; // aborted
      });
  }

  private abortLock() {
    if (!this.queued) return;
    this.queued = false;
    this.lockAbort?.abort();
    this.lockAbort = null;
  }

  private becomeLeader() {
    this.leader = true;
    this.leaderStatus = null;
    this.conn = this.makeConn();
    this.post({ t: "lead", tab: this.tab });
    this.conn.setKeys(this.unionKeys());
    this.conn.start();
    this.recompute();
  }

  private stepDown(reason: string) {
    if (!this.leader) return;
    this.leader = false;
    const conn = this.conn;
    this.conn = null;
    conn?.stop(reason);
    this.post({ t: "status", tab: this.tab, st: { state: "polling", source: null, reason: "handover" } });
    this.releaseLock?.();
    this.releaseLock = null;
    this.recompute();
  }

  private post(m: Msg) {
    try {
      this.bc?.postMessage(m);
    } catch {
      /* channel closed */
    }
  }

  private onChannel(m: Msg) {
    if (!m || typeof m !== "object" || m.tab === this.tab) return;
    const now = this.env.now();
    switch (m.t) {
      case "subs": {
        const before = this.unionKeys().join(",");
        if (m.keys.length) this.peers.set(m.tab, { keys: m.keys, at: now });
        else this.peers.delete(m.tab);
        if (this.leader && this.unionKeys().join(",") !== before) this.conn?.setKeys(this.unionKeys());
        return;
      }
      case "lead":
        this.leaderTab = m.tab;
        this.leaderSeenAt = now;
        this.post({ t: "subs", tab: this.tab, keys: this.wantLead() ? this.localKeys() : [] });
        this.followSince = now;
        return;
      case "status":
        if (this.leader) return; // two leaders cannot happen with the lock; ignore stale messages
        this.leaderTab = m.tab;
        this.leaderStatus = m.st;
        this.leaderSeenAt = now;
        return this.recompute();
      case "frame":
        if (this.leader) return;
        this.leaderTab = m.tab;
        this.leaderSeenAt = now;
        this.dispatch(m.kind, m.p, true);
        return;
      case "bye":
        this.peers.delete(m.tab);
        if (m.tab === this.leaderTab) {
          this.leaderStatus = null;
          this.leaderTab = null;
          this.recompute();
        }
        if (this.leader) this.conn?.setKeys(this.unionKeys());
        return;
    }
  }

  // ---- frames / status ----------------------------------------------------------------------------
  private dispatch(kind: "ticker" | "kline", p: Record<string, unknown>, relayed = false) {
    if (this.expectedEx && p.ex !== this.expectedEx) return;
    const key = kind === "ticker" ? `ticker:${p.symbol}` : `kline:${p.symbol}:${p.tf}`;
    const set = this.listeners.get(key);
    if (!set) return;
    if (relayed) {
      const first = this.status.state !== "live";
      this.lastFrameAt = this.env.now();
      if (first) this.recompute();
    }
    for (const cb of [...set]) {
      try {
        (cb as (x: unknown) => void)(p);
      } catch {
        /* a page bug must not break the feed */
      }
    }
  }

  private recompute() {
    if (!this.enabled) return this.setStatus(OFF);
    const base = { shared: this.shared, leader: this.leader };
    const want = this.wantLead();
    if (!want) {
      const reason = this.hiddenLong() ? "hidden" : "idle";
      return this.setStatus({ state: "polling", source: this.status.source, reason, ...base });
    }
    if (!this.shared || this.leader) {
      const st = this.conn?.status ?? { state: "connecting" as const, source: null, reason: "connecting" };
      return this.setStatus({ ...st, ...base });
    }
    // follower: the leader's word + our own frames
    const now = this.env.now();
    const ls = this.leaderStatus;
    if (!ls || now - this.leaderSeenAt > LEADER_TTL_MS)
      return this.setStatus({ state: "connecting", source: null, reason: "no-leader", ...base });
    if (ls.state !== "live") return this.setStatus({ ...ls, ...base });
    const ref = Math.max(this.lastFrameAt, 0);
    if (!ref) {
      const waiting = now - this.followSince <= DATA_GAP_MS;
      return this.setStatus({ state: waiting ? "connecting" : "polling", source: ls.source, reason: waiting ? "waiting" : "stale", ...base });
    }
    if (now - ref > DATA_GAP_MS) return this.setStatus({ state: "polling", source: ls.source, reason: "stale", ...base });
    this.setStatus({ state: "live", source: ls.source, reason: null, ...base });
  }

  private setStatus(next: RtStatus) {
    const s = this.status;
    if (s.state === next.state && s.source === next.source && s.reason === next.reason && s.shared === next.shared && s.leader === next.leader) return;
    this.status = next;
    for (const cb of [...this.statusSubs]) cb();
  }
}

// ---- browser singleton ------------------------------------------------------------------------------
function wsUrl(): string {
  const env = (import.meta as unknown as { env?: Record<string, string | undefined> }).env ?? {};
  const base = (env.VITE_API_BASE ?? "").replace(/\/$/, "");
  if (base.startsWith("http")) {
    const u = new URL(base);
    u.protocol = u.protocol === "https:" ? "wss:" : "ws:";
    u.pathname = "/api/v1/ws";
    u.search = "";
    return u.toString();
  }
  return `${location.protocol === "https:" ? "wss:" : "ws:"}//${location.host}/api/v1/ws`;
}

export function browserEnv(): RtEnv {
  const nav = navigator as Navigator & { locks?: RtEnv["locks"] };
  return {
    makeWs: (url) => new WebSocket(url) as unknown as ReturnType<RtEnv["makeWs"]>,
    url: wsUrl,
    now: () => Date.now(),
    setTimeout: (fn, ms) => window.setTimeout(fn, ms),
    clearTimeout: (id) => window.clearTimeout(id as number),
    rand: Math.random,
    makeChannel: typeof BroadcastChannel !== "undefined" ? (name) => new BroadcastChannel(name) as unknown as BcLike : undefined,
    locks: nav.locks && typeof nav.locks.request === "function" ? nav.locks : undefined,
    isHidden: () => document.visibilityState === "hidden",
    onVisibility: (cb) => {
      document.addEventListener("visibilitychange", cb);
      return () => document.removeEventListener("visibilitychange", cb);
    },
    onPageHide: (cb) => {
      window.addEventListener("pagehide", cb);
      return () => window.removeEventListener("pagehide", cb);
    },
  };
}

let _hub: RtHub | null = null;
/** The tab's hub. ``VITE_RT_WS=off`` at build time keeps it permanently off (frontend kill switch). */
export function rtHub(): RtHub {
  if (!_hub) {
    const env = (import.meta as unknown as { env?: Record<string, string | undefined> }).env ?? {};
    const off = String(env.VITE_RT_WS ?? "").toLowerCase() === "off";
    // test hook: ?rtsolo=1 forces one connection per tab (used by the screenshot / latency harness)
    const solo = typeof location !== "undefined" && /[?&]rtsolo=1\b/.test(location.search);
    const e = browserEnv();
    _hub = new RtHub(solo ? { ...e, makeChannel: undefined, locks: undefined } : e, off);
  }
  return _hub;
}
