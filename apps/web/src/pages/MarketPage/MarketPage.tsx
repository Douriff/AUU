import { useState } from "react";
import { CandleChart } from "@/components/chart/CandleChart";
import { RiskTagBar } from "@/components/alerts/RiskTagBar";
import { DepthPanel } from "@/components/market/DepthPanel";
import { SymbolList } from "@/components/market/SymbolList";
import { TradesTape } from "@/components/market/TradesTape";
import { useMarketSession } from "@/hooks/useMarketSession";

export function MarketPage() {
  const [symbol, setSymbol] = useState("MOCK/USDC");
  const [interval] = useState("1m");
  const {
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
  } = useMarketSession(symbol, interval);

  return (
    <div className="market-page">
      <div className="market-top">
        <div className="ws-status" data-status={wsStatus}>
          WS {wsStatus}
          {providers.length ? ` · ${providers.join(",")}` : ""}
          {" · "}
          DATA_PROVIDER={dataProvider}
        </div>
        <RiskTagBar tags={riskTags} allow={riskAllow} />
      </div>
      <div className="market-grid">
        <aside className="left">
          <SymbolList symbols={symbols} active={symbol} onSelect={setSymbol} />
        </aside>
        <section className="center">
          <div className="chart-header">
            <strong>{symbol}</strong>
            <span className="muted">{interval}</span>
            <span className="muted">
              signals {signals.filter((s) => s.side !== "flat").length} · fills {fills.length}
            </span>
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
