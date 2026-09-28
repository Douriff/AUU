import { useEffect, useState } from "react";
import { marketProvider } from "@/providers/HttpWsProvider";
import type { BoardSnapshot } from "@/types/contracts";

/** Poll the read-only board. Keeps the last good snapshot so refreshes do not blank the page. */
export function useBoard(pollMs = 4000) {
  const [board, setBoard] = useState<BoardSnapshot | null>(null);
  const [err, setErr] = useState("");

  useEffect(() => {
    let stop = false;
    const tick = async () => {
      try {
        const data = await marketProvider.getBoard();
        if (!stop) {
          setBoard(data);
          setErr("");
        }
      } catch (e) {
        if (!stop) setErr(e instanceof Error ? e.message : "board failed");
      }
    };
    void tick();
    const id = window.setInterval(() => void tick(), pollMs);
    return () => {
      stop = true;
      window.clearInterval(id);
    };
  }, [pollMs]);

  return { board, err };
}
