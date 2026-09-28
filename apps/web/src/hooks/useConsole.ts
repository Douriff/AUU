import { useEffect, useRef, useState } from "react";
import { marketProvider } from "@/providers/HttpWsProvider";
import type { ConsoleEvent, ConsoleStats } from "@/types/contracts";

/** Poll the console incrementally. Keeps the log so a refresh does not blank it. */
export function useConsole(pollMs = 2000) {
  const [events, setEvents] = useState<ConsoleEvent[]>([]);
  const [stats, setStats] = useState<ConsoleStats | null>(null);
  const [err, setErr] = useState("");
  const cursor = useRef("");

  useEffect(() => {
    let stop = false;
    const tick = async () => {
      try {
        const data = await marketProvider.getEvents(cursor.current || undefined);
        if (stop) return;
        if (data.cursor) cursor.current = data.cursor;
        if (data.events.length) {
          setEvents((prev) => {
            const seen = new Set(prev.map((row) => row.id));
            const next = prev.slice();
            for (const row of data.events) {
              if (!seen.has(row.id)) {
                seen.add(row.id);
                next.push(row);
              }
            }
            next.sort((a, b) => a.ts - b.ts || a.cursor - b.cursor);
            return next.length > 2000 ? next.slice(-2000) : next;
          });
        }
        setStats(data.stats);
        setErr("");
      } catch (e) {
        if (!stop) setErr(e instanceof Error ? e.message : "console failed");
      }
    };
    void tick();
    const id = window.setInterval(() => void tick(), pollMs);
    return () => {
      stop = true;
      window.clearInterval(id);
    };
  }, [pollMs]);

  return { events, stats, err };
}
