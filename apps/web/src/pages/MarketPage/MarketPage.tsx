import { useEffect, useState } from "react";
import { CandleChart } from "@/components/chart/CandleChart";
import { RiskTagBar } from "@/components/alerts/RiskTagBar";
import { CurvePanel } from "@/components/market/CurvePanel";
import { CurveProgressBar } from "@/components/market/CurveProgressBar";
import { DepthPanel } from "@/components/market/DepthPanel";
import { SymbolList } from "@/components/market/SymbolList";
import { TradesTape } from "@/components/market/TradesTape";
import { useMarketSession } from "@/hooks/useMarketSession";
import { truncateMint, VENUE } from "@/venue";

export function MarketPage() {
  const [symbol, setSymbol] = useState("");
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
    tradingState,
    wsStatus,
    providers,
    dataProvider,
    dataSource,
    pumpSnapshot,
  } = useMarketSession(symbol, interval);

  useEffect(() => {
    if (!symbols.length) return;
    if (!symbol || !symbols.some((s) => s.symbol === symbol)) {
      setSymbol(symbols[0].symbol);
    }
  }, [symbols, symbol]);

  const info = symbols.find((s) => s.symbol === symbol);
  const ticker = info ? `${info.base}/${info.quote}` : symbol || "…";
  const venueLabel = dataProvider === "pumpfun_paper" ? VENUE : "mock";

  return (
    <div className="market-page">
      <div className="market-top">
        <div className="ws-status" data-status={wsStatus}>
          WS {wsStatus}
          {providers.length ? ` · ${providers.join(",")}` : ""}
          {" · "}
          venue={venueLabel}
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
            {info?.mint ? <span className="muted">{truncateMint(info.mint)}</span> : null}
            <span className="muted">{interval}</span>
            <span className="muted">
              signals {signals.filter((s) => s.side !== "flat").length} · fills {fills.length}
            </span>
            <CurveProgressBar snapshot={pumpSnapshot} />
          </div>
          <CandleChart candles={candles} signals={signals} fills={fills} />
        </section>
        <aside className="right">
          <CurvePanel snapshot={pumpSnapshot} />
          <DepthPanel book={book} />
          <TradesTape trades={trades} />
        </aside>
      </div>
    </div>
  );
}
