/**
 * Server sync for chart drawings. localStorage stays the instant, offline-capable copy; the server
 * copy (GET/PUT /api/v1/chart/drawings) makes drawings follow the account across devices.
 *
 * Per symbol we keep `{ base, dirty }` next to the drawings:
 *  - base  = server `updatedAt` this device last saw (undefined = never synced: legacy step-1 local drawings)
 *  - dirty = local edits not yet accepted by the server
 * Rules: unsynced legacy drawings are migrated by merging (union) with whatever the server has; a clean
 * local copy simply follows the server; a dirty copy whose server base did not move overwrites the server
 * (so deletes propagate); if both sides changed the two are merged (union - nothing is ever lost, at worst a
 * drawing deleted on one device reappears). Failures keep `dirty` and retry (timer, `online` event, next edit).
 */
import { drawingsKey, type Saved } from "./drawings";

export type SyncState = "local" | "syncing" | "synced" | "error";
export type Remote = { drawings: Saved[]; updatedAt: number };

export class SyncConflict extends Error {
  constructor(public current: Remote) {
    super("conflict");
  }
}
/** `auth` = not signed in / standby node (keep working locally, do not retry fast). */
export class SyncUnavailable extends Error {
  constructor(public kind: "auth" | "network") {
    super(kind);
  }
}

export interface SyncApi {
  get(symbol: string): Promise<Remote>;
  /** Throws SyncConflict (409) or SyncUnavailable. */
  put(symbol: string, drawings: Saved[], base: number): Promise<Remote>;
}
export type KV = Pick<Storage, "getItem" | "setItem" | "removeItem">;

type Meta = { base?: number; dirty: boolean };
const metaKey = (symbol: string) => `auu.kc.drawings.sync.v1.${symbol.toUpperCase()}`;
const fingerprint = (d: Saved) => JSON.stringify([d.name, d.points, d.extendData ?? ""]);

/** remote first, then local drawings the remote does not have yet. */
export function mergeDrawings(remote: Saved[], local: Saved[]): Saved[] {
  const seen = new Set(remote.map(fingerprint));
  const out = [...remote];
  for (const d of local) {
    const f = fingerprint(d);
    if (!seen.has(f)) {
      seen.add(f);
      out.push(d);
    }
  }
  return out;
}

export class DrawingsSync {
  private timers = new Map<string, ReturnType<typeof setTimeout>>();
  private inflight = new Map<string, Promise<void>>();
  private disposed = false;
  private onOnline = () => {
    for (const s of this.dirtySymbols) void this.push(s);
  };
  private dirtySymbols = new Set<string>();

  constructor(
    private api: SyncApi,
    private kv: KV,
    private cb: { state: (symbol: string, s: SyncState) => void; remote: (symbol: string, list: Saved[]) => void },
    private opts: { debounceMs?: number; retryMs?: number } = {},
  ) {
    if (typeof window !== "undefined") window.addEventListener("online", this.onOnline);
  }

  private readLocal(symbol: string): Saved[] {
    try {
      const raw = JSON.parse(this.kv.getItem(drawingsKey(symbol)) || "[]");
      return Array.isArray(raw) ? raw.filter((o) => o && typeof o.name === "string" && Array.isArray(o.points)) : [];
    } catch {
      return [];
    }
  }
  private writeLocal(symbol: string, list: Saved[]) {
    try {
      this.kv.setItem(drawingsKey(symbol), JSON.stringify(list));
    } catch {
      /* private mode */
    }
  }
  private readMeta(symbol: string): Meta {
    try {
      const m = JSON.parse(this.kv.getItem(metaKey(symbol)) || "null");
      if (m && typeof m === "object") return { base: typeof m.base === "number" ? m.base : undefined, dirty: !!m.dirty };
    } catch {
      /* fall through */
    }
    return { dirty: false };
  }
  private writeMeta(symbol: string, m: Meta) {
    try {
      this.kv.setItem(metaKey(symbol), JSON.stringify(m));
    } catch {
      /* private mode */
    }
    if (m.dirty) this.dirtySymbols.add(symbol);
    else this.dirtySymbols.delete(symbol);
  }

  /** Call after the local copy was rewritten by the user (draw / move / delete / clear). */
  markDirty(symbol: string): void {
    const m = this.readMeta(symbol);
    this.writeMeta(symbol, { base: m.base, dirty: true });
    this.cb.state(symbol, "syncing");
    const t = this.timers.get(symbol);
    if (t) clearTimeout(t);
    this.timers.set(symbol, setTimeout(() => void this.push(symbol), this.opts.debounceMs ?? 800));
  }

  /** On chart load: reconcile local with the server (see file header). */
  async pull(symbol: string): Promise<void> {
    let remote: Remote;
    this.cb.state(symbol, "syncing");
    try {
      remote = await this.api.get(symbol);
    } catch (e) {
      this.fail(symbol, e);
      return;
    }
    if (this.disposed) return;
    const local = this.readLocal(symbol);
    const meta = this.readMeta(symbol);
    if (meta.base === undefined) {
      // never synced on this device: legacy local drawings migrate by merge
      if (!local.length) {
        this.writeMeta(symbol, { base: remote.updatedAt, dirty: false });
        if (remote.drawings.length) {
          this.writeLocal(symbol, remote.drawings);
          this.cb.remote(symbol, remote.drawings);
        }
        this.cb.state(symbol, "synced");
        return;
      }
      const merged = mergeDrawings(remote.drawings, local);
      this.writeLocal(symbol, merged);
      if (merged.length !== local.length) this.cb.remote(symbol, merged);
      if (merged.length === remote.drawings.length) {
        this.writeMeta(symbol, { base: remote.updatedAt, dirty: false });
        this.cb.state(symbol, "synced");
        return;
      }
      this.writeMeta(symbol, { base: remote.updatedAt, dirty: true });
      await this.push(symbol);
      return;
    }
    if (meta.dirty) {
      if (remote.updatedAt === meta.base) {
        await this.push(symbol); // server unchanged since our base: local edits (incl. deletes) win
        return;
      }
      const merged = mergeDrawings(remote.drawings, local);
      this.writeLocal(symbol, merged);
      this.cb.remote(symbol, merged);
      this.writeMeta(symbol, { base: remote.updatedAt, dirty: true });
      await this.push(symbol);
      return;
    }
    if (remote.updatedAt !== meta.base) {
      this.writeLocal(symbol, remote.drawings);
      this.writeMeta(symbol, { base: remote.updatedAt, dirty: false });
      this.cb.remote(symbol, remote.drawings);
    }
    this.cb.state(symbol, "synced");
  }

  /** Send the local copy; one request at a time per symbol. */
  push(symbol: string): Promise<void> {
    const prev = this.inflight.get(symbol) ?? Promise.resolve();
    const run = prev.then(() => this.pushNow(symbol));
    this.inflight.set(symbol, run);
    void run.finally(() => this.inflight.get(symbol) === run && this.inflight.delete(symbol));
    return run;
  }

  private async pushNow(symbol: string): Promise<void> {
    const timer = this.timers.get(symbol);
    if (timer) {
      clearTimeout(timer);
      this.timers.delete(symbol);
    }
    const meta = this.readMeta(symbol);
    if (!meta.dirty) return;
    this.cb.state(symbol, "syncing");
    let sent = this.readLocal(symbol);
    try {
      let res: Remote;
      try {
        res = await this.api.put(symbol, sent, meta.base ?? 0);
      } catch (e) {
        if (!(e instanceof SyncConflict)) throw e;
        sent = mergeDrawings(e.current.drawings, sent);
        this.writeLocal(symbol, sent);
        this.cb.remote(symbol, sent);
        res = await this.api.put(symbol, sent, e.current.updatedAt);
      }
      // edits made while the request was in flight stay dirty (markDirty already armed the next push)
      const stillDirty = JSON.stringify(this.readLocal(symbol)) !== JSON.stringify(sent);
      this.writeMeta(symbol, { base: res.updatedAt, dirty: stillDirty });
      this.cb.state(symbol, stillDirty ? "syncing" : "synced");
    } catch (e) {
      this.fail(symbol, e);
    }
  }

  private fail(symbol: string, e: unknown) {
    const kind = e instanceof SyncUnavailable ? e.kind : "network";
    this.cb.state(symbol, kind === "auth" ? "local" : "error");
    if (kind === "network" && this.readMeta(symbol).dirty && !this.disposed) {
      const t = this.timers.get(symbol);
      if (t) clearTimeout(t);
      this.timers.set(symbol, setTimeout(() => void this.push(symbol), this.opts.retryMs ?? 30_000));
    }
  }

  /** Unmount: stop timers and try one last push of unsent edits. */
  dispose(): void {
    this.disposed = true;
    if (typeof window !== "undefined") window.removeEventListener("online", this.onOnline);
    for (const t of this.timers.values()) clearTimeout(t);
    this.timers.clear();
    for (const s of this.dirtySymbols) void this.pushNow(s);
  }
}
