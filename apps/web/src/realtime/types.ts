/** Wire types of the market-data part of /api/v1/ws (apps/api/app/marketdata/realtime/ws_session.py). */
export interface RtCaps {
  push: boolean;
  intervals: string[];
  symbols: string[];
  staleMs: number;
  live?: boolean;
  source?: string | null;
}

export interface RtTicker {
  symbol: string;
  ex: string;
  last: number;
  change24h: number | null;
  quoteVolume: number | null;
  ts: number | null;
  recvAt: number;
}

export interface RtKline {
  symbol: string;
  tf: string;
  ex: string;
  ts: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
  closed: boolean;
  eventTs: number | null;
  recvAt: number;
}

/** off = feature not available (switch off / no ``realtime`` in hello): pages behave exactly as before.
 *  live = frames are flowing; anything else = pages keep (or go back to) their normal polling. */
export type RtState = "off" | "connecting" | "live" | "polling";

export interface RtStatus {
  state: RtState;
  /** exchange the server streams from (rt_status.source) */
  source: string | null;
  /** why we are not live (closed / stale / rt-down / source / hidden / no-push / no-leader) */
  reason: string | null;
  /** this tab shares another tab's connection (BroadcastChannel) */
  shared: boolean;
  leader: boolean;
}

export type SubKind = "ticker" | "kline";
export interface SubSpec {
  channel: SubKind;
  symbol: string;
  interval?: string;
}

export const keyOf = (s: SubSpec) => (s.channel === "ticker" ? `ticker:${s.symbol}` : `kline:${s.symbol}:${s.interval}`);
export function specOf(key: string): SubSpec {
  const [channel, symbol, interval] = key.split(":");
  return channel === "kline" ? { channel: "kline", symbol, interval } : { channel: "ticker", symbol };
}

/** Everything that touches the browser, injectable for the unit tests (scripts/realtime.test.mjs). */
export interface WsLike {
  readyState: number;
  send(data: string): void;
  close(code?: number, reason?: string): void;
  onopen: ((ev: unknown) => void) | null;
  onmessage: ((ev: { data: unknown }) => void) | null;
  onclose: ((ev: unknown) => void) | null;
  onerror: ((ev: unknown) => void) | null;
}
export interface BcLike {
  postMessage(msg: unknown): void;
  close(): void;
  onmessage: ((ev: { data: unknown }) => void) | null;
}
export interface LocksLike {
  request(name: string, opts: { signal?: AbortSignal }, cb: () => Promise<void>): Promise<unknown>;
}
export interface RtEnv {
  makeWs: (url: string) => WsLike;
  url: () => string;
  now: () => number;
  setTimeout: (fn: () => void, ms: number) => unknown;
  clearTimeout: (id: unknown) => void;
  rand: () => number;
  makeChannel?: (name: string) => BcLike;
  locks?: LocksLike;
  isHidden: () => boolean;
  onVisibility: (cb: () => void) => () => void;
  onPageHide?: (cb: () => void) => () => void;
}
