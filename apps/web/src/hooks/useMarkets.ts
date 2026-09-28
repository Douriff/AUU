import { useEffect, useState } from "react";
import { marketProvider } from "@/providers/HttpWsProvider";
import type { MarketList } from "@/types/contracts";

/** Poll the markets list. Keeps the previous rows so a refresh does not blank the table. */
export function useMarkets(pollMs = 2500) {
  const [list, setList] = useState<MarketList | null>(null);
  const [err, setErr] = useState("");

  useEffect(() => {
    let stop = false;
    const tick = async () => {
      try {
        const data = await marketProvider.getMarkets();
        if (!stop) {
          setList(data);
          setErr("");
        }
      } catch (e) {
        if (!stop) setErr(e instanceof Error ? e.message : "markets failed");
      }
    };
    void tick();
    const id = window.setInterval(() => void tick(), pollMs);
    return () => {
      stop = true;
      window.clearInterval(id);
    };
  }, [pollMs]);

  return { list, err };
}
