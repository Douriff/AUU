import { useEffect, useMemo, useRef, useState } from "react";
import { PnlStrip } from "@/pages/PnlPage/PnlStrip";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { CoinBadge, CoinSwitcher, MarketList, Spark, fmtPct, fmtPx, fmtRate, fmtVol, pxWidths, tone } from "@/components/market/MarketList";
import { Num } from "@/components/ui/Num";
import { Empty, Sk, SkCards, SkRows } from "@/components/ui/Skeleton";
import { marketProvider } from "@/providers/HttpWsProvider";
import { CHART_TFS, MainstreamChart } from "@/components/chart/MainstreamChart";
import { MultiChart } from "@/components/chart/MultiChart";
import { createCrosshairBus } from "@/components/chart/crosshairBus";
import { cellsFor, loadLayout, normalizeCells, saveLayout, type Cell, type LayoutN, type MultiLayout } from "@/components/chart/layout";
import { PaperTradePanel } from "@/components/trade/PaperTradePanel";
import { OrderBook } from "@/components/market/OrderBook";
import type { MainstreamCoin, MainstreamTf, MarketRow, MarketsResponse } from "@/types/mainstream";
import { useTranslation } from "react-i18next";
import i18n from "i18next";
import { errText } from "@/i18n/errors";
import { fmtFixed } from "@/i18n/format";
import { rtHub } from "@/realtime/hub";
import { useRtStatus, useRtTickers } from "@/realtime/useRealtime";
import { RtBadge } from "@/components/market/RtBadge";

const pct1 = (v: number) => `${fmtFixed(v * 100, 1)}%`;
const coinReason = (c: MainstreamCoin | null | undefined, fallbackKey: string) =>
  c?.reasonCode ? i18n.t(`ms.reason.${c.reasonCode}`) : c?.reason && i18n.language === "zh-CN" ? c.reason : i18n.t(fallbackKey);

// Market list: the server keeps one shared ticker cache (a few seconds), so a 5 s poll costs the exchange nothing extra.
const POLL_MS = 5_000;
// While prices are pushed (/api/v1/ws, step 3) the list poll only re-calibrates the slow fields (7d/30d, funding,
// holdings) and acts as a safety net; it drops back to POLL_MS the moment the push is not live.
const POLL_LIVE_MS = 30_000;
// Funding history and the extra-venue coin card change slowly: refetch them every SLOW_EVERY polls (30 s as before).
const SLOW_EVERY = 6;

function FundingBars({ rows }: { rows: { ts: number; rate: number }[] }) {
  const { t } = useTranslation();
  const w = 720;
  const h = 90;
  if (!rows.length) return <Empty icon="chart" title={t("ms.noFunding")} hint={t("ms.noFundingHint")} />;
  const m = Math.max(...rows.map((r) => Math.abs(r.rate)), 1e-6);
  const bw = w / rows.length;
  const mid = h / 2;
  return (
    <svg className="ms-chart ms-funding" viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none" role="img" aria-label={t("ms.fundingHistory")}>
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
  const { t } = useTranslation();
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
        <span>{t("ms.vol24h")}</span>
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
  const { t } = useTranslation();
  return (
    <div className="ov-cards">
      {pick.map((r) => (
        <MiniCard key={r.symbol} r={r} onOpen={onOpen} />
      ))}
      <div className="ov-card">
        <h6>
          <span>{t("ms.breadth")}</span>
          <span className="dim">{t("ms.nCoins", { n: items.length })}</span>
        </h6>
        <div className="ov-breadth" aria-label={t("ms.breadthAria", { ups, downs })}>
          <i className="bg-up" style={{ flex: ups || 0.001 }} />
          <i className="bg-down" style={{ flex: downs || 0.001 }} />
        </div>
        <div className="ov-kv">
          <span>
            <b className="up num">{ups}</b> {t("ms.up")} · <b className="down num">{downs}</b> {t("ms.down")}
          </span>
          <span>
            {t("ms.avg")} <b className={`num ${tone(avg)}`}>{fmtPct(avg)}</b>
          </span>
        </div>
      </div>
      <Link to="/console" className="ov-card ov-strat">
        <h6>
          <span>{t("ms.stratCard")}</span>
          <span className="ov-go">{t("ms.details")} ›</span>
        </h6>
        <div className="ov-kv">
          <span>{t("ms.heldCoins")}</span>
          <b className="num">{held.length}</b>
        </div>
        <div className="ov-kv">
          <span>{t("ms.grossExposure")}</span>
          <b className="num">{pct1(gross)}</b>
        </div>
        <div className="ov-kv">
          <span>{t("ms.nextRebalance")}</span>
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
  const { t } = useTranslation();
  const held = rows.filter((r) => r.held).sort((a, b) => Math.abs(b.weight ?? 0) - Math.abs(a.weight ?? 0));
  if (!held.length) return <Empty icon="inbox" title={t("ms.flat")} hint={t("ms.flatHint")} />;
  const w = pxWidths(held.map((r) => r.price));
  return (
    <div className="pro-table-wrap">
      <table className="pro-table">
        <thead>
          <tr>
            <th>{t("ms.col.coin")}</th>
            <th>{t("ms.col.side")}</th>
            <th>{t("ms.col.target")}</th>
            <th>{t("ms.col.last")}</th>
            <th>24h</th>
            <th>{t("ms.d7")}</th>
            <th>{t("ms.funding")}</th>
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
              <td className={(r.weight ?? 0) < 0 ? "down" : "up"}>{(r.weight ?? 0) < 0 ? t("ms.short") : t("ms.long")}</td>
              <td className="num">{pct1(Math.abs(r.weight ?? 0))}</td>
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
  const { t: tr } = useTranslation();
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
  const slowTick = Math.floor(tick / SLOW_EVERY);
  const [bottom, setBottom] = useState<"held" | "funding">("held");
  const now = useNow();
  // multi-chart layout (1 / 2 / 4) + fullscreen chart; layout persists in localStorage
  const [layout, setLayoutState] = useState<MultiLayout>(() => loadLayout(tf));
  const setLayout = (fn: (l: MultiLayout) => MultiLayout) =>
    setLayoutState((l) => {
      const next = fn(l);
      saveLayout(next);
      return next;
    });
  const [fs, setFs] = useState(false);
  const bus = useMemo(() => createCrosshairBus(), []);
  useEffect(() => {
    document.documentElement.classList.toggle("kc-fs-lock", fs);
    if (!fs) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && !(e.target instanceof HTMLInputElement) && setFs(false);
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("keydown", onKey);
      document.documentElement.classList.remove("kc-fs-lock");
    };
  }, [fs]);

  // realtime push (off unless the markets response advertises it: then nothing below changes behaviour)
  const rt = useRtStatus();
  const rtLive = rt.state === "live";
  const rtLiveRef = useRef(rtLive);
  rtLiveRef.current = rtLive;
  useEffect(() => {
    let alive = true;
    marketProvider
      .getMainstreamMarkets()
      .then((d) => {
        if (!alive) return;
        setMk(d);
        setErr("");
        rtHub().configure(d.realtime ?? null, d.exchange);
      })
      .catch((e: unknown) => alive && setErr(errText(e, "common.loadFailed")));
    const t = window.setTimeout(() => setTick((n) => n + 1), rtLiveRef.current ? POLL_LIVE_MS : POLL_MS);
    return () => {
      alive = false;
      window.clearTimeout(t);
    };
  }, [tick]);
  // push stopped being live -> poll at once and continue at the normal 5 s cadence
  const wasLive = useRef(false);
  useEffect(() => {
    if (wasLive.current && !rtLive) setTick((n) => n + 1);
    wasLive.current = rtLive;
  }, [rtLive]);
  const rtSyms = useMemo(() => {
    const caps = mk?.realtime;
    if (!caps) return [];
    const ok = new Set(caps.symbols);
    return (mk?.items ?? []).map((r) => r.symbol).filter((s) => ok.has(s));
  }, [mk?.realtime, mk?.items]);
  const rtTk = useRtTickers(rtSyms);
  // pushed price overlays the polled row (same exchange only; hub drops other venues)
  const mkView: MarketsResponse | null = useMemo(() => {
    if (!mk || !rtLive) return mk;
    let changed = false;
    const items = mk.items.map((r) => {
      const t = rtTk[r.symbol];
      if (!t || t.recvAt < mk.asOf - 1_000) return r; // older than the polled snapshot
      changed = true;
      return { ...r, price: t.last, change24h: t.change24h ?? r.change24h, quoteVolume24h: t.quoteVolume ?? r.quoteVolume24h };
    });
    return changed ? { ...mk, items } : mk;
  }, [mk, rtTk, rtLive]);

  const poolRow = useMemo(() => mkView?.items.find((i) => i.symbol === pick), [mkView, pick]);
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
      .catch((e: unknown) => alive && setCoinErr(errText(e, "common.loadFailed")));
    return () => {
      alive = false;
    };
  }, [extraMode, pick, venueParam, slowTick]);

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
  }, [current?.symbol, slowTick]);

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
  // stable across 5 s polls: only changes when the symbol list itself changes (keeps the chart layout from re-resolving)
  const symKey = (mk?.items ?? []).map((r) => r.symbol).join(",");
  const knownSymbols = useMemo(() => (symKey ? symKey.split(",") : []), [symKey]);
  const effLayout = useMemo(() => normalizeCells(layout, knownSymbols, current?.symbol ?? pick ?? ""), [layout, knownSymbols, current?.symbol, pick]);
  const multi = effLayout.n > 1;
  const onCell = (i: number, c: Cell) => {
    if (i === 0) {
      if (c.symbol !== current?.symbol) open(c.symbol);
      if (c.tf !== tf) pickTf(c.tf);
    } else setLayout((l) => ({ ...l, extra: l.extra.map((x, k) => (k === i - 1 ? c : x)) as MultiLayout["extra"] }));
  };

  if (!current) {
    return (
      <div className="pro-page mainstream-page">
        {err ? (
          <div className="pro-alert">
            {tr("ms.loadFailed", { err })}
            <button type="button" className="btn-ghost" onClick={() => setTick((n) => n + 1)}>
              {tr("common.retry")}
            </button>
          </div>
        ) : null}
        {!mk && !err ? <OverviewSkeleton /> : null}
        {mk?.tickers.error ? <div className="pro-alert is-warn">{tr("ms.tickerFallback")}</div> : null}
        {mk && pick && extraMode && !coinErr ? <OverviewSkeleton /> : null}
        {mk && pick && (!extraMode || coinErr) ? (
          <div className="pro-alert">
            {coinErr ? tr("ms.coinErr", { sym: pick, err: coinErr }) : tr("ms.unknownCoin", { sym: pick })}
            <button type="button" className="btn-ghost ms-back" onClick={back}>
              {tr("ms.backToList")}
            </button>
          </div>
        ) : null}
        {mk && !(pick && extraMode && !coinErr) ? (
          <>
            <Overview mk={mkView ?? mk} onOpen={open} now={now} />
            <MarketList rows={(mkView ?? mk).items} onOpen={open} quote={quote} />
            <p className="ml-foot">
              {tr("ms.foot", { n: mk.items.length, src: (mk.exchange || "—").toUpperCase() })}
              <RtBadge />
            </p>
          </>
        ) : null}
      </div>
    );
  }

  return (
    <div className="pro-page mainstream-page is-detail">
      <PnlStrip />
      <section className="ms-panel">
        <div className="tk">
          <button type="button" className="ms-back" onClick={back} aria-label={tr("ms.backAria")}>
            ‹
          </button>
          <CoinSwitcher rows={(mkView ?? mk)!.items} current={current.symbol} onPick={open} quote={quote} />
          {extraMode ? (
            <span className="tk-src" title={tr("ms.srcTitle")}>
              {venueLabel(dataVenue) || venueLabel(venueParam) || "—"}
              {coin && !coin.inPool ? <em>{tr("ms.notInPool")}</em> : null}
              {coin && coin.viewOnly ? <em className="warn">{tr("ms.viewOnly")}</em> : null}
            </span>
          ) : null}
          <div className="tk-last">
            <b className={`num ${tone(current.change24h)}`} data-testid="tk-price">{fmtPx(current.price)}</b>
            <em className={`num ${tone(current.change24h)}`}>{fmtPct(current.change24h)}</em>
            {extraMode ? null : <RtBadge />}
          </div>
          <div className="tk-stats">
            <span className="tk-st">
              <small>{tr("ms.vol24h")}</small>
              <b className="num">{fmtVol(current.quoteVolume24h)}</b>
            </span>
            <span className="tk-st">
              <small>{tr("ms.d7")}</small>
              <b className={`num ${tone(current.change7d)}`}>{fmtPct(current.change7d)}</b>
            </span>
            <span className="tk-st">
              <small>{tr("ms.d30")}</small>
              <b className={`num ${tone(current.change30d)}`}>{fmtPct(current.change30d)}</b>
            </span>
            <span className="tk-st">
              <small>{tr("ms.fundingCountdown")}</small>
              <b className="num">
                <span className={tone(current.funding)}>{fmtRate(current.funding)}</span> <span className="dim">{countdown(current.nextFundingMs, now)}</span>
              </b>
            </span>
            <span className="tk-st">
              <small>{tr("ms.stratPos")}</small>
              <b className="num">{current.held ? `${(current.weight ?? 0) < 0 ? tr("ms.short") : tr("ms.long")} ${pct1(Math.abs(current.weight ?? 0))}` : "—"}</b>
            </span>
          </div>
        </div>
        <div className={`ms-trade-grid${multi ? " is-multi" : ""}`}>
          <div className={`ms-trade-chart pane${fs ? " is-fs" : ""}`}>
            <div className="ph">
              {multi ? null : (
                <div className="mk-tabs ms-tfs" role="tablist" aria-label={tr("ms.tfAria")}>
                  {CHART_TFS.map((t) => (
                    <button key={t.id} type="button" role="tab" aria-selected={tf === t.id} className={tf === t.id ? "is-on" : ""} onClick={() => pickTf(t.id)}>
                      {tr(t.label)}
                    </button>
                  ))}
                </div>
              )}
              <div className="ms-lay" role="group" aria-label={tr("chart.layout.aria")}>
                {([1, 2, 4] as LayoutN[]).map((n) => (
                  <button
                    key={n}
                    type="button"
                    className={effLayout.n === n ? "is-on" : ""}
                    aria-pressed={effLayout.n === n}
                    title={tr(`chart.layout.${n}`)}
                    aria-label={tr(`chart.layout.${n}`)}
                    data-testid={`layout-${n}`}
                    onClick={() => setLayout((l) => ({ ...l, n }))}
                  >
                    <svg viewBox="0 0 20 14" width="20" height="14" aria-hidden="true" fill="none" stroke="currentColor" strokeWidth="1.3">
                      <rect x="1" y="1" width="18" height="12" rx="1.5" />
                      {n >= 2 ? <path d="M10 1v12" /> : null}
                      {n === 4 ? <path d="M1 7h18" /> : null}
                    </svg>
                  </button>
                ))}
                {multi ? (
                  <button
                    type="button"
                    className={`ms-lay-sync${effLayout.sync ? " is-on" : ""}`}
                    aria-pressed={effLayout.sync}
                    title={tr("chart.layout.syncTitle")}
                    data-testid="layout-sync"
                    onClick={() => setLayout((l) => ({ ...l, sync: !l.sync }))}
                  >
                    ⌖ <span>{tr("chart.layout.sync")}</span>
                  </button>
                ) : null}
                <button
                  type="button"
                  className={`ms-lay-fs${fs ? " is-on" : ""}`}
                  aria-pressed={fs}
                  title={tr(fs ? "chart.layout.fsExit" : "chart.layout.fsEnter")}
                  aria-label={tr(fs ? "chart.layout.fsExit" : "chart.layout.fsEnter")}
                  data-testid="chart-fs"
                  onClick={() => setFs((v) => !v)}
                >
                  {fs ? "✕" : "⛶"}
                </button>
              </div>
            </div>
            {extraMode && !dataVenue ? (
              <div className="ms-viewonly-chart">
                <b>{tr("ms.viewOnly")}</b>
                <span>{coinReason(coin, "ms.noKline")}</span>
              </div>
            ) : multi ? (
              <MultiChart
                n={effLayout.n}
                cells={cellsFor(effLayout, { symbol: current.symbol, tf })}
                symbols={knownSymbols}
                venue={dataVenue}
                overlay={!extraMode || Boolean(coin?.inPool)}
                bus={effLayout.sync ? bus : null}
                onChange={onCell}
              />
            ) : (
              <MainstreamChart symbol={current.symbol} tf={tf} venue={dataVenue} overlay={!extraMode || Boolean(coin?.inPool)} />
            )}
          </div>
          <div className="ms-trade-book pane">
            {extraMode && !dataVenue ? <div className="ob-empty dim">{tr("ob.none")}</div> : <OrderBook symbol={current.symbol} venue={dataVenue} />}
          </div>
          <div className="ms-trade-side pane">
            {extraMode && coin && !coin.tradable ? (
              <div className="ms-viewonly">
                <b>{tr("ms.viewOnly")}</b>
                <p>{coinReason(coin, "ms.noPaper")}</p>
                <p className="dim">{tr("ms.paperScope")}</p>
              </div>
            ) : (
              <PaperTradePanel symbol={current.symbol} price={current.price} />
            )}
          </div>
        </div>
        <div className="ms-bottom pane">
          <div className="ph ms-bottom-tabs" role="tablist">
            <button type="button" role="tab" aria-selected={bottom === "held"} className={bottom === "held" ? "is-on" : ""} onClick={() => setBottom("held")}>
              {tr("ms.stratPos")} <span className="dim">{mk!.items.filter((r) => r.held).length}</span>
            </button>
            <button type="button" role="tab" aria-selected={bottom === "funding"} className={bottom === "funding" ? "is-on" : ""} onClick={() => setBottom("funding")}>
              {tr("ms.funding")}
            </button>
            {bottom === "funding" ? (
              <span className="dim ms-legend">
                {current.perp} · {tr("ms.lastN", { n: funding.length })} · <i className="sw bg-up" /> {tr("ms.posLongs")} <i className="sw bg-down" /> {tr("ms.negShorts")}
              </span>
            ) : (
              <span className="dim ms-legend">{tr("ms.heldLegend")}</span>
            )}
          </div>
          {bottom === "held" ? <HeldTable rows={mk!.items} onOpen={open} /> : <div className="ms-funding-wrap"><FundingBars rows={funding} /></div>}
        </div>
      </section>
    </div>
  );
}
