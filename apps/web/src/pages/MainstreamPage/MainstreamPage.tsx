import { useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { CoinSwitcher, MarketList, fmtPct, fmtPx, fmtRate, fmtVol, tone } from "@/components/market/MarketList";
import { marketProvider } from "@/providers/HttpWsProvider";
import { CHART_TFS, MainstreamChart } from "@/components/chart/MainstreamChart";
import { PaperTradePanel } from "@/components/trade/PaperTradePanel";
import type { MainstreamFreshness, MainstreamTf, MarketsResponse } from "@/types/mainstream";

const POLL_MS = 30_000;





function fmtTime(ms: number | null | undefined): string {
  if (!ms) return "—";
  return new Date(ms).toLocaleString("zh-CN", { hour12: false, month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
}


function FundingBars({ rows }: { rows: { ts: number; rate: number }[] }) {
  const w = 720;
  const h = 90;
  if (!rows.length) return <p className="mj-err">暂无资金费率数据。</p>;
  const m = Math.max(...rows.map((r) => Math.abs(r.rate)), 1e-6);
  const bw = w / rows.length;
  const mid = h / 2;
  return (
    <svg className="ms-chart ms-funding" viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none" role="img" aria-label="资金费率历史">
      <line x1="0" x2={w} y1={mid} y2={mid} stroke="rgba(255,255,255,0.15)" />
      {rows.map((r, i) => {
        const bh = (Math.abs(r.rate) / m) * (mid - 4);
        return (
          <rect
            key={r.ts}
            x={i * bw + 0.5}
            width={Math.max(bw - 1, 1)}
            y={r.rate >= 0 ? mid - bh : mid}
            height={Math.max(bh, 0.5)}
            fill={r.rate >= 0 ? "#3ee08f" : "#ff5d5d"}
          />
        );
      })}
    </svg>
  );
}

function FreshnessLine({ f }: { f: MainstreamFreshness }) {
  const blocked = Object.keys(f.blocked || {});
  return (
    <div className="ms-fresh">
      <span className={f.stale ? "is-down" : "is-ok"}>{f.stale ? "数据过期" : "数据新鲜"}</span>
      <span>来源 {f.exchange ? f.exchange.toUpperCase() : "—"}</span>
      <span>上次刷新 {fmtTime(f.lastRefreshMs)}</span>
      {blocked.length ? <span className="is-warn">不可用: {blocked.join(", ").toUpperCase()}</span> : null}
      {f.stale && f.staleSeries.length ? <span className="is-warn">过期: {f.staleSeries.join(", ")}</span> : null}
      {Object.keys(f.gaps || {}).length ? <span className="is-warn">缺口: {Object.entries(f.gaps).map(([k, v]) => `${k.split(":").slice(1).join(":")}×${v}`).join(", ")}</span> : null}
    </div>
  );
}

/** 主流行情: market list of every strategy coin (exchange-style), then the coin's chart + paper panel. */
export function MainstreamPage() {
  const [params, setParams] = useSearchParams();
  const pick = (params.get("symbol") || "").toUpperCase();
  const [mk, setMk] = useState<MarketsResponse | null>(null);
  const [fresh, setFresh] = useState<MainstreamFreshness | null>(null);
  const [err, setErr] = useState("");
  const [tf, setTf] = useState<MainstreamTf>(() => {
    const saved = (params.get("tf") ?? window.localStorage.getItem("auu.ms.tf")) as MainstreamTf | null;
    return saved && CHART_TFS.some((t) => t.id === saved) ? saved : "1h";
  });
  const [funding, setFunding] = useState<{ ts: number; rate: number }[]>([]);
  const [tick, setTick] = useState(0);

  useEffect(() => {
    let alive = true;
    marketProvider
      .getMainstreamMarkets()
      .then((d) => alive && (setMk(d), setErr("")))
      .catch((e: unknown) => alive && setErr(e instanceof Error ? e.message : "读取失败"));
    marketProvider
      .getMainstreamStatus()
      .then((f) => alive && setFresh(f))
      .catch(() => undefined);
    const t = window.setTimeout(() => setTick((n) => n + 1), POLL_MS);
    return () => {
      alive = false;
      window.clearTimeout(t);
    };
  }, [tick]);

  const current = useMemo(() => mk?.items.find((i) => i.symbol === pick), [mk, pick]);

  useEffect(() => {
    if (!current) return;
    let alive = true;
    void marketProvider
      .getMainstreamFunding(current.symbol, 90)
      .then((d) => alive && setFunding(d.funding))
      .catch(() => alive && setFunding([]));
    return () => {
      alive = false;
    };
  }, [current?.symbol, tick]);

  const open = (symbol: string) => {
    const next = new URLSearchParams(params);
    next.set("symbol", symbol);
    setParams(next);
    window.scrollTo({ top: 0 });
  };
  const back = () => {
    const next = new URLSearchParams(params);
    next.delete("symbol");
    setParams(next);
  };
  const pickTf = (id: MainstreamTf) => {
    setTf(id);
    window.localStorage.setItem("auu.ms.tf", id);
  };
  const quote = mk?.quote || "USDT";

  return (
    <div className="majors-page mj-dense mainstream-page">
      <header className="mj-top">
        <div>
          <h1>{current ? "主流行情 · 交易" : "主流行情"}</h1>
          <p>交易所公开行情与永续资金费率（只读，无 API key）。纸面交易，实盘锁定。</p>
        </div>
        <div className="mj-health">
          <span className="mj-badge">LIVE OFF</span>
        </div>
      </header>
      {err ? <p className="mj-err">{err}</p> : null}
      {!mk && !err ? <p className="mj-err">正在读取行情…</p> : null}
      {fresh ? <FreshnessLine f={fresh} /> : null}
      {mk && !current ? (
        <>
          <MarketList rows={mk.items} onOpen={open} quote={quote} />
          <p className="ml-foot muted">
            {mk.items.length} 个币（趋势策略币池）· 价格来源 {mk.tickers.source === "ticker" ? "交易所 24h 行情（批量，30 秒缓存）" : "本地 K 线库（每 5 分钟刷新）"} ·{" "}
            {(mk.exchange || "—").toUpperCase()} · 30 天走势为日线收盘
          </p>
        </>
      ) : null}
      {mk && pick && !current ? <p className="mj-err">未知币种 {pick}，<button type="button" className="ms-back" onClick={back}>返回列表</button></p> : null}
      {mk && current ? (
        <section className="ms-panel">
          <div className="mj-tools ms-detail-bar">
            <button type="button" className="ms-back" onClick={back} aria-label="返回行情列表">
              ← 行情
            </button>
            <CoinSwitcher rows={mk.items} current={current.symbol} onPick={open} quote={quote} />
            <span className="ms-detail-px num">{fmtPx(current.price)}</span>
            <span className="muted ms-detail-meta">
              24h 成交额 {fmtVol(current.quoteVolume24h)} · 资金费 <em className={tone(current.funding)}>{fmtRate(current.funding)}</em> · 30 天{" "}
              <em className={tone(current.change30d)}>{fmtPct(current.change30d)}</em>
              {current.held ? ` · 策略持仓 ${((current.weight ?? 0) * 100).toFixed(1)}%` : ""}
            </span>
          </div>
          <div className="mj-tools">
            <div className="mk-tabs ms-tfs" role="tablist" aria-label="周期">
              {CHART_TFS.map((t) => (
                <button key={t.id} type="button" role="tab" aria-selected={tf === t.id} className={tf === t.id ? "is-on" : ""} onClick={() => pickTf(t.id)}>
                  {t.label}
                </button>
              ))}
            </div>
          </div>
          <div className="ms-trade-grid">
            <div className="ms-trade-chart">
              <MainstreamChart symbol={current.symbol} tf={tf} />
            </div>
            <PaperTradePanel symbol={current.symbol} price={current.price} />
          </div>
          <div className="mj-tools ms-sub">
            <b>{current.perp} 资金费率</b>
            <span className="muted">最近 {funding.length} 期 · 绿=多头付费 红=空头付费</span>
          </div>
          <FundingBars rows={funding} />
        </section>
      ) : null}
    </div>
  );
}
