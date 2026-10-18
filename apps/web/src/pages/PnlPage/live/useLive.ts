import { useCallback, useEffect, useRef, useState } from "react";
import { marketProvider, type ExchangeAvailability, type ExchangeConfig, type ExchangePosition } from "@/providers/HttpWsProvider";

export type Market = "spot" | "swap";
export type Balance = { asset: string; total: number; free: number; used: number };

/** Reasons that still allow READING balances / positions (the account is linked and the feature is on). */
const READABLE = new Set<string | null>([null, "KEY_NO_TRADE", "KEY_NO_FUTURES", "KILL_SWITCH"]);
export const canRead = (reason: string | null | undefined) => READABLE.has(reason ?? null);

export const STABLES = new Set(["USDT", "USDC", "BUSD", "FDUSD", "TUSD", "USDP", "DAI"]);

/** Live-view symbols: spot "BTC/USDT", USDT perpetual "BTC/USDT:USDT". */
export function normSymbol(raw: string, market: Market): string {
  let s = raw.trim().toUpperCase().replace("-", "/");
  if (!s) return "";
  if (!s.includes("/")) s += "/USDT";
  if (market === "swap" && !s.includes(":")) s += ":USDT";
  return s;
}

/** Availability of every exchange x market for the signed-in user (computed by the server, same rules as the order endpoints). */
export function useAvailability(everyMs = 30_000) {
  const [data, setData] = useState<ExchangeAvailability | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [cfg, setCfg] = useState<ExchangeConfig | null>(null);
  const load = useCallback(() => {
    marketProvider.exchangeAvailability().then((d) => { setData(d); setError(null); }).catch(setError);
  }, []);
  // caps / max leverage change rarely: read once (the server limits reads to 60/min per user)
  useEffect(() => { marketProvider.exchangeConfig().then(setCfg).catch(() => undefined); }, []);
  useEffect(() => {
    load();
    const id = window.setInterval(() => !document.hidden && load(), everyMs);
    return () => window.clearInterval(id);
  }, [load, everyMs]);
  return { data, cfg, error, reload: load };
}

/** Balances (spot) or positions (futures) of one exchange; polled while visible, only when reading is allowed. */
export function useLiveRows(ex: string, market: Market, enabled: boolean, everyMs = 30_000) {
  const [balances, setBalances] = useState<Balance[] | null>(null);
  const [positions, setPositions] = useState<ExchangePosition[] | null>(null);
  const [supported, setSupported] = useState<Record<string, boolean>>({});
  const [error, setError] = useState<unknown>(null);
  const checked = useRef("");
  const load = useCallback(() => {
    if (!enabled) return;
    if (market === "spot") {
      marketProvider.exchangeBalances(ex)
        .then(async (r) => {
          setBalances(r.items);
          setError(null);
          const assets = r.items.filter((b) => !STABLES.has(b.asset)).map((b) => b.asset);
          const sig = `${ex}:${assets.join(",")}`;
          if (assets.length && sig !== checked.current) {
            checked.current = sig;
            const s = await marketProvider.exchangeSupported(ex, assets, "spot").catch(() => null);
            if (s) setSupported(s.supported);
            else checked.current = "";
          }
        })
        .catch(setError);
    } else {
      marketProvider.exchangeFuturesPositions(ex).then((r) => { setPositions(r.items); setError(null); }).catch(setError);
    }
  }, [ex, market, enabled]);
  useEffect(() => {
    setBalances(null);
    setPositions(null);
    setError(null);
    load();
    const id = window.setInterval(() => !document.hidden && load(), everyMs);
    return () => window.clearInterval(id);
  }, [load, everyMs]);
  return { balances, positions, supported, error, reload: load };
}
