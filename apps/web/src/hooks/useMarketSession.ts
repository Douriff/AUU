import { useCallback, useEffect, useRef, useState } from "react";
import { marketProvider } from "@/providers/HttpWsProvider";
import type {
  BookSnapshot,
  Candle,
  Fill,
  RiskEvent,
  SignalOut,
  SymbolInfo,
  TradeTick,
} from "@/types/contracts";

export function useMarketSession(symbol: string, interval = "1m") {
  const [symbols, setSymbols] = useState<SymbolInfo[]>([]);
  const [candles, setCandles] = useState<Candle[]>([]);
  const [signals, setSignals] = useState<SignalOut[]>([]);
  const [fills, setFills] = useState<Fill[]>([]);
  const [book, setBook] = useState<BookSnapshot | null>(null);
  const [trades, setTrades] = useState<TradeTick[]>([]);
  const [riskTags, setRiskTags] = useState<string[]>([]);
  const [riskAllow, setRiskAllow] = useState<boolean | null>(null);
  const [wsStatus, setWsStatus] = useState<string>("closed");
  const [providers, setProviders] = useState<string[]>([]);
  const [dataProvider, setDataProvider] = useState("mock");
  const symbolRef = useRef(symbol);
  symbolRef.current = symbol;

  useEffect(() => {
    marketProvider.listSymbols().then(setSymbols).catch(console.error);
    marketProvider
      .getHealth()
      .then((h) => setDataProvider(h.provider))
      .catch(() => undefined);
  }, []);

  const loadHistory = useCallback(async (sym: string, iv: string) => {
    const [c, s, f] = await Promise.all([
      marketProvider.getCandles(sym, iv),
      marketProvider.getSignals(sym),
      marketProvider.getFills(sym),
    ]);
    setCandles(c);
    setSignals(s);
    setFills(f);
    setTrades([]);
    setBook(null);
  }, []);

  useEffect(() => {
    loadHistory(symbol, interval).catch(console.error);
  }, [symbol, interval, loadHistory]);

  useEffect(() => {
    const unsub = marketProvider.connect({
      onStatus: setWsStatus,
      onHello: (h) => setProviders(h.providers),
      onCandle: (c) => {
        if (c.symbol !== symbolRef.current) return;
        setCandles((prev) => {
          if (!prev.length) return [c];
          const last = prev[prev.length - 1];
          if (last.t === c.t) {
            const next = prev.slice();
            next[next.length - 1] = c;
            return next;
          }
          if (c.t > last.t) return [...prev, c];
          return prev;
        });
      },
      onBook: (b) => {
        if (b.symbol !== symbolRef.current) return;
        setBook(b);
      },
      onTrade: (t) => {
        if (t.symbol !== symbolRef.current) return;
        setTrades((prev) => [t, ...prev].slice(0, 40));
      },
      onSignal: (s) => {
        if (s.symbol && s.symbol !== symbolRef.current) return;
        setSignals((prev) => [...prev, s]);
      },
      onFill: (f) => {
        setFills((prev) => [...prev, f]);
      },
      onRisk: (r: RiskEvent) => {
        if (r.symbol && r.symbol !== symbolRef.current) return;
        setRiskAllow(r.risk.allow);
        setRiskTags(r.risk.tags ?? []);
      },
    });

    return unsub;
  }, []);

  useEffect(() => {
    marketProvider.subscribe("candles", symbol, interval);
    marketProvider.subscribe("book", symbol);
    marketProvider.subscribe("trades", symbol);
    marketProvider.subscribe("signals", symbol, interval);
    marketProvider.subscribe("fills", symbol);
    marketProvider.subscribe("risk", symbol);
  }, [symbol, interval]);

  return {
    symbols,
    candles,
    signals,
    fills,
    book,
    trades,
    riskTags,
    riskAllow,
    wsStatus,
    providers,
    dataProvider,
  };
}
