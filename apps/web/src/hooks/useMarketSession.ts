import { useCallback, useEffect, useRef, useState } from "react";
import { marketProvider } from "@/providers/HttpWsProvider";
import { useDataSource } from "@/hooks/useDataSource";
import type {
  BookSnapshot,
  Candle,
  Fill,
  RejectEvent,
  RiskEvent,
  SignalOut,
  SymbolInfo,
  TradeTick,
  TradingState,
} from "@/types/contracts";

export function useMarketSession(symbol: string, interval = "1m") {
  const { dataSource } = useDataSource();
  const [symbols, setSymbols] = useState<SymbolInfo[]>([]);
  const [candles, setCandles] = useState<Candle[]>([]);
  const [signals, setSignals] = useState<SignalOut[]>([]);
  const [fills, setFills] = useState<Fill[]>([]);
  const [book, setBook] = useState<BookSnapshot | null>(null);
  const [trades, setTrades] = useState<TradeTick[]>([]);
  const [riskTags, setRiskTags] = useState<string[]>([]);
  const [riskAllow, setRiskAllow] = useState<boolean | null>(null);
  const [tradingState, setTradingState] = useState<TradingState>("active");
  const [wsStatus, setWsStatus] = useState<string>("closed");
  const [providers, setProviders] = useState<string[]>([]);
  const [dataProvider, setDataProvider] = useState("mock");
  const symbolRef = useRef(symbol);
  const dataSourceRef = useRef(dataSource);
  symbolRef.current = symbol;
  dataSourceRef.current = dataSource;

  useEffect(() => {
    marketProvider.listSymbols().then(setSymbols).catch(console.error);
    marketProvider
      .getHealth()
      .then((h) => {
        setDataProvider(h.provider);
        if (h.trading_state === "active" || h.trading_state === "reducing" || h.trading_state === "halted") {
          setTradingState(h.trading_state);
        }
      })
      .catch(() => undefined);
  }, []);

  const loadHistory = useCallback(async (sym: string, iv: string, src: typeof dataSource) => {
    const [c, s] = await Promise.all([
      marketProvider.getCandles(sym, iv),
      marketProvider.getSignals(sym),
    ]);
    setCandles(c);
    setSignals(s);
    setTrades([]);
    setBook(null);
    // paper: do not seed mock fills on chart — wait for PaperBroker WS fills
    if (src === "paper") {
      setFills([]);
    } else {
      const f = await marketProvider.getFills(sym);
      setFills(f);
    }
  }, []);

  useEffect(() => {
    loadHistory(symbol, interval, dataSource).catch(console.error);
  }, [symbol, interval, dataSource, loadHistory]);

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
        // keep mock signal overlays in both modes
        setSignals((prev) => [...prev, s]);
      },
      onFill: (f) => {
        // paper mode: only draw hub/paper fills (tag paper* or symbol match from hub)
        if (dataSourceRef.current === "paper") {
          const tag = f.tag ?? "";
          const fromPaper = tag.startsWith("paper") || tag.includes("paper") || Boolean(f.symbol);
          if (!fromPaper) return; // ignore mock stream fills
        }
        setFills((prev) => [...prev, f]);
      },
      onRisk: (r: RiskEvent) => {
        if (r.symbol && r.symbol !== symbolRef.current) return;
        setRiskAllow(r.risk.allow);
        setRiskTags(r.risk.tags ?? []);
      },
      onReject: (r: RejectEvent) => {
        if (r.symbol && r.symbol !== symbolRef.current) return;
        // reject only — never fabricate / draw a Fill
        setRiskAllow(false);
        setRiskTags(r.tags ?? []);
      },
      onTradingState: (t) => {
        setTradingState(t.state);
        if (t.reason) {
          setRiskTags((prev) => (prev.includes(t.reason!) ? prev : [...prev, t.reason!]));
        }
      },
    });

    return unsub;
  }, []);

  useEffect(() => {
    marketProvider.subscribe("candles", symbol, interval);
    marketProvider.subscribe("book", symbol);
    marketProvider.subscribe("trades", symbol);
    marketProvider.subscribe("signals", symbol, interval);
    marketProvider.subscribe("risk", symbol);
    if (dataSource === "paper") {
      // still subscribe fills so hub-forwarded events arrive; mock pump also runs —
      // onFill filters mock when paper
      marketProvider.subscribe("fills", symbol);
    } else {
      marketProvider.subscribe("fills", symbol);
    }
  }, [symbol, interval, dataSource]);

  return {
    symbols,
    candles,
    signals,
    fills,
    book,
    trades,
    riskTags,
    riskAllow,
    tradingState,
    wsStatus,
    providers,
    dataProvider,
    dataSource,
  };
}
