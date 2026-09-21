import { useEffect, useState } from "react";
import { marketProvider } from "@/providers/HttpWsProvider";
import type { PaperPerformance } from "@/types/contracts";

export function usePaperPerformance(pollMs = 2500, mc = false) {
  const [stats, setStats] = useState<PaperPerformance | null>(null);
  const [err, setErr] = useState("");

  useEffect(() => {
    let stop = false;
    const tick = async () => {
      try {
        const data = await marketProvider.getPaperPerformance("session", { mc });
        if (!stop) {
          setStats(data);
          setErr("");
        }
      } catch (e) {
        if (!stop) setErr(e instanceof Error ? e.message : "stats failed");
      }
    };
    void tick();
    const id = window.setInterval(() => void tick(), pollMs);
    return () => {
      stop = true;
      window.clearInterval(id);
    };
  }, [pollMs, mc]);

  return { stats, err };
}
