import { useEffect, useState } from "react";
import { marketProvider } from "@/providers/HttpWsProvider";
import type { PaperPerformance } from "@/types/contracts";

export function usePaperPerformance(pollMs = 2500) {
  const [stats, setStats] = useState<PaperPerformance | null>(null);
  const [err, setErr] = useState("");

  useEffect(() => {
    let stop = false;
    const tick = async () => {
      try {
        const data = await marketProvider.getPaperPerformance();
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
    const onFill = () => {
      void tick();
    };
    window.addEventListener("auu:fill", onFill);
    return () => {
      stop = true;
      window.clearInterval(id);
      window.removeEventListener("auu:fill", onFill);
    };
  }, [pollMs]);

  return { stats, err };
}
