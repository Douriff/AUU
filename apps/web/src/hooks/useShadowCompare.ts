import { useCallback, useEffect, useState } from "react";
import { marketProvider } from "@/providers/HttpWsProvider";
import type { ShadowCompareReport } from "@/types/contracts";

export function useShadowCompare(pollMs = 0) {
  const [report, setReport] = useState<ShadowCompareReport | null>(null);
  const [err, setErr] = useState("");
  const [loading, setLoading] = useState(false);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const data = await marketProvider.getShadowCompare();
      setReport(data);
      setErr("");
    } catch (e) {
      setErr(e instanceof Error ? e.message : "shadow compare failed");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    let stop = false;
    const tick = async () => {
      if (stop) return;
      await refresh();
    };
    void tick();
    if (pollMs > 0) {
      const id = window.setInterval(() => void tick(), pollMs);
      return () => {
        stop = true;
        window.clearInterval(id);
      };
    }
    return () => {
      stop = true;
    };
  }, [pollMs, refresh]);

  return { report, err, loading, refresh };
}
