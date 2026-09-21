import { useEffect, useState } from "react";
import { marketProvider } from "@/providers/HttpWsProvider";
import type { ExecutabilityReport } from "@/types/contracts";

export function useExecutability(pollMs = 2500) {
  const [report, setReport] = useState<ExecutabilityReport | null>(null);
  const [err, setErr] = useState("");

  useEffect(() => {
    let stop = false;
    const tick = async () => {
      try {
        const data = await marketProvider.getExecutability("session");
        if (!stop) {
          setReport(data);
          setErr("");
        }
      } catch (e) {
        if (!stop) setErr(e instanceof Error ? e.message : "executability failed");
      }
    };
    void tick();
    const id = window.setInterval(() => void tick(), pollMs);
    return () => {
      stop = true;
      window.clearInterval(id);
    };
  }, [pollMs]);

  return { report, err };
}
