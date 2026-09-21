import { useEffect, useState } from "react";
import { CandleChart } from "@/components/chart/CandleChart";
import { RiskTagBar } from "@/components/alerts/RiskTagBar";
import { CurvePanel } from "@/components/market/CurvePanel";
import { DepthPanel } from "@/components/market/DepthPanel";
import { SymbolList } from "@/components/market/SymbolList";
import { TradesTape } from "@/components/market/TradesTape";
import { useMarketSession } from "@/hooks/useMarketSession";
import { marketProvider } from "@/providers/HttpWsProvider";
import type { CurveSnapshot } from "@/types/contracts";
import { DEFAULT_SYMBOL, truncateMint, VENUE } from "@/venue";

export function MarketPage() {
  const [symbol, setSymbol] = useState(DEFAULT_SYMBOL);
  const [interval] = useState("1m");
  const [curve, setCurve] = useState<CurveSnapshot | null>(null);
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
  } = useMarketSession(symbol, interval);

  useEffect(() => {
    marketProvider.getCurve(symbol).then(setCurve).catch(() => setCurve(null));
  }, [symbol]);

  const info = symbols.find((s) => s.symbol === symbol);
  const ticker = info ? `${info.base}/${info.quote}` : truncateMint(symbol);

  return (
    <div className="market-page">
      <div className="market-top">
        <div className="ws-status" data-status={wsStatus}>
          WS {wsStatus}
          {providers.length ? ` · ${providers.join(",")}` : ""}
          {" · "}
          venue={VENUE}
          {" · "}
          DATA_PROVIDER={dataProvider}
          {" · "}
          dataSource={dataSource}
          {" · "}
          state={tradingState}
        </div>
        <RiskTagBar tags={riskTags} allow={riskAllow} />
      </div>
      <div className="market-grid">
        <aside className="left">
          <SymbolList symbols={symbols} active={symbol} onSelect={setSymbol} />
        </aside>
        <section className="center">
          <div className="chart-header">
            <strong>{ticker}</strong>
            <span className="muted">{truncateMint(info?.mint ?? symbol)}</span>
            <span className="muted">{interval}</span>
            <span className="muted">
              signals {signals.filter((s) => s.side !== "flat").length} · fills {fills.length}
            </span>
          </div>
          <CandleChart candles={candles} signals={signals} fills={fills} />
        </section>
        <aside className="right">
          <CurvePanel curve={curve} />
          <DepthPanel book={book} />
          <TradesTape trades={trades} />
        </aside>
      </div>
    </div>
  );
}
