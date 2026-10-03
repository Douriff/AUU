import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { CoinBadge, CoinSwitcher, MarketList, Spark, fmtPct, fmtPx, fmtRate, fmtVol, pxWidths, tone } from "@/components/market/MarketList";
import { Num } from "@/components/ui/Num";
import { Empty, Sk, SkCards, SkRows } from "@/components/ui/Skeleton";
import { marketProvider } from "@/providers/HttpWsProvider";
import { CHART_TFS, MainstreamChart } from "@/components/chart/MainstreamChart";
import { PaperTradePanel } from "@/components/trade/PaperTradePanel";
import { OrderBook } from "@/components/market/OrderBook";
import type { MainstreamCoin, MainstreamTf, MarketRow, MarketsResponse } from "@/types/mainstream";

const POLL_MS = 30_000;

function FundingBars({ rows }: { rows: { ts: number; rate: number }[] }) {
  const w = 720;
  const h = 90;
  if (!rows.length) return <Empty icon="chart" title="暂无资金费率数据" hint="该币种的永续资金费率尚未同步" />;
  const m = Math.max(...rows.map((r) => Math.abs(r.rate)), 1e-6);
  const bw = w / rows.length;
  const mid = h / 2;
  return (
    <svg className="ms-chart ms-funding" viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none" role="img" aria-label="资金费率历史">
      <line x1="0" x2={w} y1={mid} y2={mid} className="ms-zero" />
      {rows.map((r, i) => {
        const bh = (Math.abs(r.rate) / m) * (mid - 4);
        return (
          <rect
            key={r.ts}
            x={i * bw + 0.5}
            width={Math.max(bw - 1, 1)}
            y={r.rate >= 0 ? mid - bh : mid}
            height={Math.max(bh, 0.5)}
            className={r.rate >= 0 ? "fill-up" : "fill-down"}
          />
        );
      })}
    </svg>
  );
}

function countdown(ms: number | null | undefined, now: number): string {
  if (!ms) return "—";
  const s = Math.max(0, Math.floor((ms - now) / 1000));
  const p = (n: number) => String(n).padStart(2, "0");
  return `${p(Math.floor(s / 3600))}:${p(Math.floor((s % 3600) / 60))}:${p(s % 60)}`;
}

/** Next 08:00 Beijing time (= 00:00 UTC), the daily strategy rebalance. */
function nextRebalance(now: number): number {
  return (Math.floor(now / 86400_000) + 1) * 86400_000;
}

function useNow(ms = 1000) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = window.setInterval(() => setNow(Date.now()), ms);
    return () => window.clearInterval(t);
  }, [ms]);
  return now;
}

function MiniCard({ r, onOpen }: { r: MarketRow; onOpen: (s: string) => void }) {
  return (
    <button type="button" className="ov-card ov-coin" onClick={() => onOpen(r.symbol)}>
      <h6>
        <span>
          <CoinBadge symbol={r.symbol} size={16} /> {r.symbol}
          <span className="dim">/USDT</span>
        </span>
        <em className={tone(r.change24h)}>{fmtPct(r.change24h)}</em>
      </h6>
      <div className="ov-mini">
        <span className={`ov-px num ${tone(r.change24h)}`}>{fmtPx(r.price)}</span>
        <Spark values={r.spark30 || []} w={78} h={26} />
      </div>
      <div className="ov-kv">
        <span>24h 成交额</span>
        <b className="num">{fmtVol(r.quoteVolume24h)}</b>
      </div>
    </button>
  );
}

function Overview({ mk, onOpen, now }: { mk: MarketsResponse; onOpen: (s: string) => void; now: number }) {
  const items = mk.items;
  const pick = ["BTC", "ETH", "SOL"].map((s) => items.find((r) => r.symbol === s)).filter(Boolean) as MarketRow[];
  const ups = items.filter((r) => (r.change24h ?? 0) > 0).length;
  const downs = items.filter((r) => (r.change24h ?? 0) < 0).length;
  const withChg = items.filter((r) => r.change24h != null);
  const avg = withChg.length ? withChg.reduce((a, r) => a + (r.change24h ?? 0), 0) / withChg.length : null;
  const held = items.filter((r) => r.held);
  const gross = held.reduce((a, r) => a + Math.abs(r.weight ?? 0), 0);
  return (
    <div className="ov-cards">
      {pick.map((r) => (
        <MiniCard key={r.symbol} r={r} onOpen={onOpen} />
      ))}
      <div className="ov-card">
        <h6>
          <span>市场宽度 · 24h</span>
          <span className="dim">{items.length} 币</span>
        </h6>
        <div className="ov-breadth" aria-label={`上涨 ${ups} 下跌 ${downs}`}>
          <i className="bg-up" style={{ flex: ups || 0.001 }} />
          <i className="bg-down" style={{ flex: downs || 0.001 }} />
        </div>
        <div className="ov-kv">
          <span>
            <b className="up num">{ups}</b> 涨 · <b className="down num">{downs}</b> 跌
          </span>
          <span>
            均值 <b className={`num ${tone(avg)}`}>{fmtPct(avg)}</b>
          </span>
        </div>
      </div>
      <Link to="/console" className="ov-card ov-strat">
        <h6>
          <span>趋势策略 · 纸面</span>
          <span className="ov-go">详情 ›</span>
        </h6>
        <div className="ov-kv">
          <span>持仓币数</span>
          <b className="num">{held.length}</b>
        </div>
        <div className="ov-kv">
          <span>总敞口</span>
          <b className="num">{(gross * 100).toFixed(1)}%</b>
        </div>
        <div className="ov-kv">
          <span>下次调仓 08:00</span>
          <b className="num">{countdown(nextRebalance(now), now)}</b>
        </div>
      </Link>
    </div>
  );
}

function OverviewSkeleton() {
  return (
    <>
      <SkCards n={5} h={92} />
      <div className="ml">
        <div className="ml-bar">
          <Sk w={180} h={14} />
        </div>
        <SkRows rows={10} cols={7} />
      </div>
    </>
  );
}

function HeldTable({ rows, onOpen }: { rows: MarketRow[]; onOpen: (s: string) => void }) {
  const held = rows.filter((r) => r.held).sort((a, b) => Math.abs(b.weight ?? 0) - Math.abs(a.weight ?? 0));
  if (!held.length) return <Empty icon="inbox" title="策略当前空仓" hint="趋势策略每日 08:00 调仓，信号触发后这里会列出持仓" />;
  const w = pxWidths(held.map((r) => r.price));
  return (
    <div className="pro-table-wrap">
      <table className="pro-table">
        <thead>
          <tr>
            <th>币种</th>
            <th>方向</th>
            <th>目标权重</th>
            <th>最新价</th>
            <th>24h</th>
            <th>7 天</th>
            <th>资金费率</th>
          </tr>
        </thead>
        <tbody>
          {held.map((r) => (
            <tr key={r.symbol} onClick={() => onOpen(r.symbol)} className="is-link">
              <td>
                <span className="pair">
                  <CoinBadge symbol={r.symbol} size={16} />
                  <b>{r.symbol}</b>
                </span>
              </td>
              <td className={(r.weight ?? 0) < 0 ? "down" : "up"}>{(r.weight ?? 0) < 0 ? "空" : "多"}</td>
              <td className="num">{(Math.abs(r.weight ?? 0) * 100).toFixed(1)}%</td>
              <td className="num">
                <Num text={fmtPx(r.price)} int={w.int} frac={w.frac} />
              </td>
              <td className={`num ${tone(r.change24h)}`}>{fmtPct(r.change24h)}</td>
              <td className={`num ${tone(r.change7d)}`}>{fmtPct(r.change7d)}</td>
              <td className={`num ${tone(r.funding)}`}>{fmtRate(r.funding)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** 主流行情: market overview + list of every strategy coin, then the coin's trading view (chart + paper panel). */
export function MainstreamPage() {
  const [params, setParams] = useSearchParams();
  const navigate = useNavigate();
  const pick = (params.get("symbol") || "").toUpperCase();
  const venueParam = (params.get("venue") || "").toLowerCase() || null;
  const [coin, setCoin] = useState<MainstreamCoin | null>(null);
  const [coinErr, setCoinErr] = useState("");
  const [mk, setMk] = useState<MarketsResponse | null>(null);
  const [err, setErr] = useState("");
  const [tf, setTf] = useState<MainstreamTf>(() => {
    const saved = (params.get("tf") ?? window.localStorage.getItem("auu.ms.tf")) as MainstreamTf | null;
    return saved && CHART_TFS.some((t) => t.id === saved) ? saved : "1h";
  });
  const [funding, setFunding] = useState<{ ts: number; rate: number }[]>([]);
  const [tick, setTick] = useState(0);
  const [bottom, setBottom] = useState<"held" | "funding">("held");
  const now = useNow();

  useEffect(() => {
    let alive = true;
    marketProvider
      .getMainstreamMarkets()
      .then((d) => alive && (setMk(d), setErr("")))
      .catch((e: unknown) => alive && setErr(e instanceof Error ? e.message : "读取失败"));
    const t = window.setTimeout(() => setTick((n) => n + 1), POLL_MS);
    return () => {
      alive = false;
      window.clearTimeout(t);
    };
  }, [tick]);

  const poolRow = useMemo(() => mk?.items.find((i) => i.symbol === pick), [mk, pick]);
  // 大盘 coin (outside the pool), or a pool coin opened from another venue's tab: on-demand data path.
  const extraMode = Boolean(pick && mk && (!poolRow || (venueParam && venueParam !== mk.exchange && (venueParam === "binance" || venueParam === "okx"))));

  useEffect(() => {
    if (!extraMode) {
      setCoin(null);
      setCoinErr("");
      return;
    }
    let alive = true;
    marketProvider
      .getMainstreamCoin(pick, venueParam)
      .then((c) => alive && (setCoin(c), setCoinErr("")))
      .catch((e: unknown) => alive && setCoinErr(e instanceof Error ? e.message : "读取失败"));
    return () => {
      alive = false;
    };
  }, [extraMode, pick, venueParam, tick]);

  const current: MarketRow | undefined = useMemo(() => {
    if (!extraMode) return poolRow;
    if (!coin || coin.symbol !== pick) return undefined;
    return {
      ...(poolRow ?? { symbol: coin.symbol, pair: coin.pair, perp: "", display: false, strategy: false, held: false, weight: null }),
      price: coin.price ?? poolRow?.price ?? null,
      change24h: coin.change24h ?? poolRow?.change24h ?? null,
      quoteVolume24h: coin.quoteVolume24h ?? poolRow?.quoteVolume24h ?? null,
    };
  }, [extraMode, poolRow, coin, pick]);
  const dataVenue = extraMode ? coin?.dataVenue ?? null : null;
  const venueLabel = (v: string | null | undefined) => (v ? ({ binance: "Binance", okx: "OKX", bybit: "Bybit", coinbase: "Coinbase" } as Record<string, string>)[v] ?? v : "");

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
    next.delete("venue"); // the coin switcher / held table open pool coins on the strategy source
    setParams(next);
    window.scrollTo({ top: 0 });
  };
  const back = () => {
    const next = new URLSearchParams(params);
    next.delete("symbol");
    const from = next.get("venue");
    next.delete("venue");
    if (from) {
      navigate("/majors");
      return;
    }
    setParams(next);
  };
  const pickTf = (id: MainstreamTf) => {
    setTf(id);
    window.localStorage.setItem("auu.ms.tf", id);
  };
  const quote = mk?.quote || "USDT";

  if (!current) {
    return (
      <div className="pro-page mainstream-page">
        {err ? (
          <div className="pro-alert">
            行情读取失败：{err}
            <button type="button" className="btn-ghost" onClick={() => setTick((n) => n + 1)}>
              重试
            </button>
          </div>
        ) : null}
        {!mk && !err ? <OverviewSkeleton /> : null}
        {mk?.tickers.error ? <div className="pro-alert is-warn">实时价格暂不可用，已改用本地 K 线收盘价（每 5 分钟刷新）。</div> : null}
        {mk && pick && extraMode && !coinErr ? <OverviewSkeleton /> : null}
        {mk && pick && (!extraMode || coinErr) ? (
          <div className="pro-alert">
            {coinErr ? `${pick}：${coinErr}` : `未知币种 ${pick}`}
            <button type="button" className="btn-ghost ms-back" onClick={back}>
              返回列表
            </button>
          </div>
        ) : null}
        {mk && !(pick && extraMode && !coinErr) ? (
          <>
            <Overview mk={mk} onOpen={open} now={now} />
            <MarketList rows={mk.items} onOpen={open} quote={quote} />
            <p className="ml-foot">
              {mk.items.length} 个币 · 趋势策略币池 · 数据来源 {(mk.exchange || "—").toUpperCase()} 公开行情 · 30 天走势为日线收盘
            </p>
          </>
        ) : null}
      </div>
    );
  }

  return (
    <div className="pro-page mainstream-page is-detail">
      <section className="ms-panel">
        <div className="tk">
          <button type="button" className="ms-back" onClick={back} aria-label="返回行情列表">
            ‹
          </button>
          <CoinSwitcher rows={mk!.items} current={current.symbol} onPick={open} quote={quote} />
          {extraMode ? (
            <span className="tk-src" title="数据来源交易所">
              {venueLabel(dataVenue) || venueLabel(venueParam) || "—"}
              {coin && !coin.inPool ? <em>大盘币 · 非策略币池</em> : null}
              {coin && coin.viewOnly ? <em className="warn">仅查看</em> : null}
            </span>
          ) : null}
          <div className="tk-last">
            <b className={`num ${tone(current.change24h)}`}>{fmtPx(current.price)}</b>
            <em className={`num ${tone(current.change24h)}`}>{fmtPct(current.change24h)}</em>
          </div>
          <div className="tk-stats">
            <span className="tk-st">
              <small>24h 成交额</small>
              <b className="num">{fmtVol(current.quoteVolume24h)}</b>
            </span>
            <span className="tk-st">
              <small>7 天</small>
              <b className={`num ${tone(current.change7d)}`}>{fmtPct(current.change7d)}</b>
            </span>
            <span className="tk-st">
              <small>30 天</small>
              <b className={`num ${tone(current.change30d)}`}>{fmtPct(current.change30d)}</b>
            </span>
            <span className="tk-st">
              <small>资金费率 / 倒计时</small>
              <b className="num">
                <span className={tone(current.funding)}>{fmtRate(current.funding)}</span> <span className="dim">{countdown(current.nextFundingMs, now)}</span>
              </b>
            </span>
            <span className="tk-st">
              <small>策略持仓</small>
              <b className="num">{current.held ? `${(current.weight ?? 0) < 0 ? "空" : "多"} ${(Math.abs(current.weight ?? 0) * 100).toFixed(1)}%` : "—"}</b>
            </span>
          </div>
        </div>
        <div className="ms-trade-grid">
          <div className="ms-trade-chart pane">
            <div className="ph">
              <div className="mk-tabs ms-tfs" role="tablist" aria-label="周期">
                {CHART_TFS.map((t) => (
                  <button key={t.id} type="button" role="tab" aria-selected={tf === t.id} className={tf === t.id ? "is-on" : ""} onClick={() => pickTf(t.id)}>
                    {t.label}
                  </button>
                ))}
              </div>
            </div>
            {extraMode && !dataVenue ? (
              <div className="ms-viewonly-chart">
                <b>仅查看</b>
                <span>{coin?.reason || "该币暂无 K 线数据"}</span>
              </div>
            ) : (
              <MainstreamChart symbol={current.symbol} tf={tf} venue={dataVenue} overlay={!extraMode || Boolean(coin?.inPool)} />
            )}
          </div>
          <div className="ms-trade-book pane">
            {extraMode && !dataVenue ? <div className="ob-empty dim">暂无订单簿</div> : <OrderBook symbol={current.symbol} venue={dataVenue} />}
          </div>
          <div className="ms-trade-side pane">
            {extraMode && coin && !coin.tradable ? (
              <div className="ms-viewonly">
                <b>仅查看</b>
                <p>{coin.reason || "该币暂不支持纸面交易"}</p>
                <p className="dim">纸面交易支持：趋势策略 19 币 + Binance / OKX 成交额前 100 的现货。</p>
              </div>
            ) : (
              <PaperTradePanel symbol={current.symbol} price={current.price} />
            )}
          </div>
        </div>
        <div className="ms-bottom pane">
          <div className="ph ms-bottom-tabs" role="tablist">
            <button type="button" role="tab" aria-selected={bottom === "held"} className={bottom === "held" ? "is-on" : ""} onClick={() => setBottom("held")}>
              策略持仓 <span className="dim">{mk!.items.filter((r) => r.held).length}</span>
            </button>
            <button type="button" role="tab" aria-selected={bottom === "funding"} className={bottom === "funding" ? "is-on" : ""} onClick={() => setBottom("funding")}>
              资金费率
            </button>
            {bottom === "funding" ? (
              <span className="dim ms-legend">
                {current.perp} · 最近 {funding.length} 期 · <i className="sw bg-up" /> 正 = 多头付费 <i className="sw bg-down" /> 负 = 空头付费
              </span>
            ) : (
              <span className="dim ms-legend">趋势策略纸面持仓（每日 08:00 调仓）</span>
            )}
          </div>
          {bottom === "held" ? <HeldTable rows={mk!.items} onOpen={open} /> : <div className="ms-funding-wrap"><FundingBars rows={funding} /></div>}
        </div>
      </section>
    </div>
  );
}
