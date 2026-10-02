import { useEffect, useState } from "react";
import { marketProvider } from "@/providers/HttpWsProvider";

/** `null` until /health answers; `true` only when the API runs with AUU_LEGACY_PUMP=on. */
let cached: boolean | null = null;
let inflight: Promise<boolean> | null = null;

function load(): Promise<boolean> {
  if (!inflight) {
    inflight = marketProvider
      .getHealth()
      .then((h) => {
        cached = h.legacyPump === true;
        return cached;
      })
      .catch(() => {
        inflight = null;
        return cached ?? false;
      });
  }
  return inflight;
}

export function useLegacyMode(): boolean | null {
  const [legacy, setLegacy] = useState<boolean | null>(cached);
  useEffect(() => {
    let alive = true;
    void load().then((v) => {
      if (alive) setLegacy(v);
    });
    return () => {
      alive = false;
    };
  }, []);
  return legacy;
}
