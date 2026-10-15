import { ApiError, marketProvider } from "@/providers/HttpWsProvider";
import { SyncConflict, SyncUnavailable, type Remote, type SyncApi } from "./drawingsSync";
import type { Saved } from "./drawings";

/** Map HTTP failures onto the sync engine's three outcomes (see drawingsSync.ts). */
function mapError(e: unknown): never {
  if (e instanceof ApiError) {
    if (e.status === 409 && e.code === "CONFLICT") {
      const cur = (e.details as { current?: Remote } | undefined)?.current;
      if (cur && Array.isArray(cur.drawings) && typeof cur.updatedAt === "number") throw new SyncConflict({ drawings: cur.drawings as Saved[], updatedAt: cur.updatedAt });
    }
    // not signed in, standby node (503 STANDBY), rejected payload: keep working locally, no retry loop
    if (e.status === 401 || e.status === 403 || e.status === 400 || e.status === 413 || e.code === "STANDBY") throw new SyncUnavailable("auth");
  }
  throw new SyncUnavailable("network");
}

export const drawingsRemote: SyncApi = {
  async get(symbol) {
    try {
      const d = await marketProvider.getChartDrawings(symbol);
      return { drawings: d.drawings as Saved[], updatedAt: d.updatedAt };
    } catch (e) {
      return mapError(e);
    }
  },
  async put(symbol, drawings, base) {
    try {
      const d = await marketProvider.putChartDrawings(symbol, drawings, base);
      return { drawings: d.drawings as Saved[], updatedAt: d.updatedAt };
    } catch (e) {
      return mapError(e);
    }
  },
};
