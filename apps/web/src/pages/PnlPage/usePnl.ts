import { useCallback, useEffect, useState } from "react";
import { ApiError, marketProvider } from "@/providers/HttpWsProvider";
import type { PnlSummary } from "@/types/contracts";

/** Polls the server-side P&L summary (default every 10 s, paused while the tab is hidden). */
export function usePnlSummary(everyMs = 10_000) {
  const [data, setData] = useState<PnlSummary | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [needLogin, setNeedLogin] = useState(false);
  const load = useCallback(() => {
    marketProvider
      .getPnlSummary()
      .then((d) => {
        setData(d);
        setError(null);
      })
      .catch((e: unknown) => {
        if (e instanceof ApiError && e.status === 401) setNeedLogin(true);
        else setError(e);
      });
  }, []);
  useEffect(() => {
    load();
    const id = window.setInterval(() => {
      if (!document.hidden) load();
    }, everyMs);
    return () => window.clearInterval(id);
  }, [load, everyMs]);
  return { data, error, needLogin, reload: load };
}

export const tone = (v: number | null | undefined) => (v == null || Math.abs(v) < 1e-9 ? "" : v > 0 ? "up" : "down");
