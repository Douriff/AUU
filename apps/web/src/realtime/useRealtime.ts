/** React bindings for the realtime hub. */
import { useEffect, useState, useSyncExternalStore } from "react";
import { rtHub } from "./hub";
import type { RtStatus, RtTicker } from "./types";

export function useRtStatus(): RtStatus {
  const hub = rtHub();
  return useSyncExternalStore(hub.onStatus, hub.getStatus, hub.getStatus);
}

/** Latest pushed ticker per symbol (batched to one React update per animation frame). Empty while the feature
 *  is off or ``symbols`` is empty; values are only meaningful while :func:`useRtStatus` is ``live``. */
export function useRtTickers(symbols: string[]): Record<string, RtTicker> {
  const [map, setMap] = useState<Record<string, RtTicker>>({});
  const key = symbols.join(",");
  useEffect(() => {
    if (!key) {
      setMap({});
      return;
    }
    const hub = rtHub();
    let pending: Record<string, RtTicker> = {};
    let raf = 0;
    const flush = () => {
      raf = 0;
      const p = pending;
      pending = {};
      setMap((m) => ({ ...m, ...p }));
    };
    const off = hub.subscribeTicker(key.split(","), (t) => {
      pending[t.symbol] = t;
      if (!raf) raf = window.requestAnimationFrame(flush);
    });
    return () => {
      off();
      if (raf) window.cancelAnimationFrame(raf);
    };
  }, [key]);
  return map;
}
