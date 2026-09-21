import { useCallback, useEffect, useState } from "react";
import { marketProvider } from "@/providers/HttpWsProvider";
import type { PumpPaperParams, PumpPaperState, TradingState } from "@/types/contracts";

const EVT = "auu:strategy";

function apply(data: PumpPaperState, setState: (s: PumpPaperState) => void) {
  setState(data);
}

export function useStrategyConfig() {
  const [state, setState] = useState<PumpPaperState | null>(null);
  const [err, setErr] = useState<string>("");

  const refresh = useCallback(async () => {
    try {
      const data = await marketProvider.getStrategy();
      setState(data);
      setErr("");
      return data;
    } catch (e) {
      setErr(e instanceof Error ? e.message : "strategy fetch failed");
      return null;
    }
  }, []);

  useEffect(() => {
    void refresh();
    const t = window.setInterval(() => void refresh(), 2500);
    const onCustom = (e: Event) => {
      const d = (e as CustomEvent<PumpPaperState>).detail;
      if (d?.params) setState(d);
    };
    const onHalt = (e: Event) => {
      const s = (e as CustomEvent<TradingState>).detail;
      if (s === "active" || s === "reducing" || s === "halted") {
        setState((prev) => (prev ? { ...prev, trading_state: s } : prev));
      }
    };
    window.addEventListener(EVT, onCustom);
    window.addEventListener("auu:tradingState", onHalt);
    return () => {
      window.clearInterval(t);
      window.removeEventListener(EVT, onCustom);
      window.removeEventListener("auu:tradingState", onHalt);
    };
  }, [refresh]);

  const patch = useCallback(async (next: Partial<PumpPaperParams>) => {
    const data = await marketProvider.putStrategy(next);
    apply(data, setState);
    window.dispatchEvent(new CustomEvent(EVT, { detail: data }));
    return data;
  }, []);

  const setAutoPaperOrders = useCallback(
    (v: boolean) => patch({ auto_paper_orders: v }),
    [patch]
  );

  const tradingState: TradingState = state?.trading_state ?? "active";
  const autoPaperOrders = state?.auto_paper_orders ?? false;
  const params = state?.params ?? null;

  return {
    state,
    params,
    tradingState,
    autoPaperOrders,
    err,
    refresh,
    patch,
    setAutoPaperOrders,
  };
}
