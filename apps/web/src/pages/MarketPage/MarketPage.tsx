import { useEffect, useState } from "react";
import { CandleChart } from "@/components/chart/CandleChart";
import { RiskTagBar } from "@/components/alerts/RiskTagBar";
import { CurveProgressBar } from "@/components/market/CurveProgressBar";
import { DepthPanel } from "@/components/market/DepthPanel";
import { SymbolList } from "@/components/market/SymbolList";
import { TradesTape } from "@/components/market/TradesTape";
import { WatchlistTable } from "@/components/market/WatchlistTable";
import { useMarketSession } from "@/hooks/useMarketSession";
import { useStrategyConfig } from "@/hooks/useStrategyConfig";
import { marketProvider } from "@/providers/HttpWsProvider";
import type { MonitorRow } from "@/types/contracts";

export function MarketPage() {
  const [symbol, setSymbol] = useState("");
  const [interval] = useState("1m");
  const [monitor, setMonitor] = useState<MonitorRow[]>([]);
  const {
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
    pumpSnapshot,
  } = useMarketSession(symbol, interval);
  const { autoPaperOrders, setAutoPaperOrders } = useStrategyConfig();

  useEffect(() => {
    if (!symbols.length) return;
    if (!symbol || !symbols.some((s) => s.symbol === symbol)) {
      setSymbol(symbols[0].symbol);
    }
  }, [symbols, symbol]);

  useEffect(() => {
    let stop = false;
    const tick = async () => {
      try {
        const rows = await marketProvider.getMonitor();
        if (!stop) setMonitor(rows);
      } catch {
        /* monitor optional on mock */
      }
    };
    void tick();
    const id = window.setInterval(() => void tick(), 1000);
    return () => {
      stop = true;
      window.clearInterval(id);
    };
  }, []);

  const watchRows =
    monitor.length > 0
      ? monitor
      : symbols.map((s) => ({
          symbol: s.symbol,
          mint: s.mint ?? "",
          kind: s.kind,
          progress_bps: null,
          phase: null,
          complete: false,
          migrated: false,
          price_sol: null,
          buy_notional_1m: 0,
          sell_notional_1m: 0,
          trade_count_1m: 0,
          estimated_impact_bps: null,
          tags: [s.kind ?? "meme_mock"],
        }));

  return (
    <div className="market-page">
      <div className="market-top">
        <div className="ws-status" data-status={wsStatus}>
          WS {wsStatus}
          {providers.length ? ` · ${providers.join(",")}` : ""}
          {" · "}
          DATA_PROVIDER={dataProvider}
          {" · "}
          dataSource={dataSource}
        </div>
        <label className="auto-paper-toggle">
          <input
            type="checkbox"
            checked={autoPaperOrders}
            onChange={(e) => void setAutoPaperOrders(e.target.checked)}
          />
          auto_paper_orders
        </label>
        <span className="trading-state" data-state={tradingState} title="RiskGate trading_state">
          {tradingState}
        </span>
        <RiskTagBar tags={riskTags} allow={riskAllow} />
      </div>
      <div className="market-grid">
        <aside className="left">
          {watchRows.some((r) => r.progress_bps != null) ? (
            <WatchlistTable rows={watchRows} active={symbol} onSelect={setSymbol} />
          ) : (
            <SymbolList symbols={symbols} active={symbol} onSelect={setSymbol} />
          )}
        </aside>
        <section className="center">
          <div className="chart-header">
            <strong>{symbol || "…"}</strong>
            <span className="muted">{interval}</span>
            <span className="muted">
              signals {signals.filter((s) => s.side !== "flat").length} · fills {fills.length}
            </span>
            <CurveProgressBar snapshot={pumpSnapshot} />
          </div>
          <CandleChart candles={candles} signals={signals} fills={fills} />
        </section>
        <aside className="right">
          <DepthPanel book={book} />
          <TradesTape trades={trades} />
        </aside>
      </div>
    </div>
  );
}
