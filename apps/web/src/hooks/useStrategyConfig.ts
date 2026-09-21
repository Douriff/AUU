import { useCallback, useEffect, useState } from "react";
import { marketProvider } from "@/providers/HttpWsProvider";
import type { PumpPaperParams, PumpPaperState, TradingState } from "@/types/contracts";

type Listener = () => void;

let snapshot: PumpPaperState | null = null;
let lastError = "";
let lastPatchAt = 0;
const listeners = new Set<Listener>();
let pollTimer: number | null = null;
let inflight = 0;

function emit() {
  for (const fn of listeners) fn();
}

function applyServer(data: PumpPaperState, fromPatch = false) {
  if (!fromPatch && Date.now() - lastPatchAt < 2000) return;
  snapshot = data;
  lastError = "";
  emit();
}

async function refresh(): Promise<PumpPaperState | null> {
  const gen = inflight;
  try {
    const data = await marketProvider.getStrategy();
    if (gen !== inflight && Date.now() - lastPatchAt < 2000) return snapshot;
    applyServer(data, false);
    return data;
  } catch (e) {
    lastError = e instanceof Error ? e.message : "strategy fetch failed";
    emit();
    return null;
  }
}

function ensurePoll() {
  if (pollTimer != null) return;
  void refresh();
  pollTimer = window.setInterval(() => void refresh(), 2500);
}

function stopPollIfIdle() {
  if (listeners.size === 0 && pollTimer != null) {
    window.clearInterval(pollTimer);
    pollTimer = null;
  }
}

export async function patchStrategy(next: Partial<PumpPaperParams>): Promise<PumpPaperState> {
  inflight += 1;
  lastPatchAt = Date.now();
  if (snapshot) {
    snapshot = {
      ...snapshot,
      params: { ...snapshot.params, ...next },
      auto_paper_orders: next.auto_paper_orders ?? snapshot.auto_paper_orders,
    };
    emit();
  }
  try {
    const data = await marketProvider.putStrategy(next);
    lastPatchAt = Date.now();
    snapshot = data;
    lastError = "";
    emit();
    return data;
  } catch (e) {
    lastError = e instanceof Error ? e.message : "strategy save failed";
    emit();
    void refresh();
    throw e;
  }
}

export function useStrategyConfig() {
  const [, bump] = useState(0);

  useEffect(() => {
    const fn: Listener = () => bump((n) => n + 1);
    listeners.add(fn);
    ensurePoll();
    const onHalt = (e: Event) => {
      const s = (e as CustomEvent<TradingState>).detail;
      if (s !== "active" && s !== "reducing" && s !== "halted") return;
      if (!snapshot) return;
      snapshot = { ...snapshot, trading_state: s };
      emit();
    };
    window.addEventListener("auu:tradingState", onHalt);
    return () => {
      listeners.delete(fn);
      window.removeEventListener("auu:tradingState", onHalt);
      stopPollIfIdle();
    };
  }, []);

  const patch = useCallback((next: Partial<PumpPaperParams>) => patchStrategy(next), []);
  const setAutoPaperOrders = useCallback((v: boolean) => patchStrategy({ auto_paper_orders: v }), []);

  return {
    state: snapshot,
    params: snapshot?.params ?? null,
    tradingState: (snapshot?.trading_state ?? "active") as TradingState,
    autoPaperOrders: snapshot?.auto_paper_orders ?? false,
    err: lastError,
    refresh,
    patch,
    setAutoPaperOrders,
  };
}
