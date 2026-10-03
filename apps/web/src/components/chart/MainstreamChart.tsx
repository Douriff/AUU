import { useCallback, useEffect, useRef, useState } from "react";
import {
  ColorType,
  CrosshairMode,
  createChart,
  type CandlestickData,
  type HistogramData,
  type IChartApi,
  type ISeriesApi,
  type LogicalRange,
  type MouseEventParams,
  type Time,
  type UTCTimestamp,
} from "lightweight-charts";
import { marketProvider } from "@/providers/HttpWsProvider";
import type { MainstreamCandle, MainstreamTf } from "@/types/mainstream";
import { CHART_CHROME, chartColors, onColorPref } from "@/theme/colorPref";

/** lightweight-charts (Apache-2.0) candlestick + volume for the mainstream page. */

export const CHART_TFS: { id: MainstreamTf; label: string }[] = [
  { id: "1m", label: "1分" },
  { id: "5m", label: "5分" },
  { id: "15m", label: "15分" },
  { id: "1h", label: "1时" },
  { id: "4h", label: "4时" },
  { id: "1d", label: "日" },
];

const INTRADAY = new Set<MainstreamTf>(["1m", "5m", "15m"]);
const PAGE = 500;
const POLL_MS: Record<MainstreamTf, number> = { "1m": 10_000, "5m": 15_000, "15m": 20_000, "1h": 60_000, "4h": 60_000, "1d": 60_000 };

// The chart renders timestamps as UTC; shift by the browser offset so the axis shows local time.
const TZ_SHIFT_SEC = -new Date().getTimezoneOffset() * 60;
const toTime = (ms: number) => (Math.floor(ms / 1000) + TZ_SHIFT_SEC) as UTCTimestamp;
const fromTime = (t: Time) => ((t as number) - TZ_SHIFT_SEC) * 1000;

function priceFormat(px: number) {
  const precision = px >= 1000 ? 2 : px >= 10 ? 2 : px >= 1 ? 3 : 5;
  return { type: "price" as const, precision, minMove: 10 ** -precision };
}

function fmt(n: number, d: number) {
  return n.toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
}

function fmtVol(v: number) {
  if (v >= 1e9) return `${(v / 1e9).toFixed(2)}B`;
  if (v >= 1e6) return `${(v / 1e6).toFixed(2)}M`;
  if (v >= 1e3) return `${(v / 1e3).toFixed(2)}K`;
  return v.toFixed(2);
}

function fmtTs(ms: number, tf: MainstreamTf) {
  const d = new Date(ms);
  const p = (n: number) => String(n).padStart(2, "0");
  const day = `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
  return tf === "1d" ? day : `${day} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

const bar = (c: MainstreamCandle): CandlestickData => ({ time: toTime(c.ts), open: c.open, high: c.high, low: c.low, close: c.close });
const vol = (c: MainstreamCandle): HistogramData => {
  const col = chartColors();
  return { time: toTime(c.ts), value: c.volume, color: c.close >= c.open ? col.upVol : col.downVol };
};
const candleColors = () => {
  const { up, down } = chartColors();
  return { upColor: up, downColor: down, borderUpColor: up, borderDownColor: down, wickUpColor: up, wickDownColor: down };
};

type Legend = { c: MainstreamCandle; prev?: MainstreamCandle };

export function MainstreamChart({ symbol, tf }: { symbol: string; tf: MainstreamTf }) {
  const hostRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const candleRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const volRef = useRef<ISeriesApi<"Histogram"> | null>(null);
  const dataRef = useRef<MainstreamCandle[]>([]);
  const byTs = useRef<Map<number, number>>(new Map());
  const loadingOld = useRef(false);
  const exhausted = useRef(false);
  const gen = useRef(0);
  const [legend, setLegend] = useState<Legend | null>(null);
  const [state, setState] = useState<{ loading: boolean; err: string; limited: boolean; retention?: number; count: number }>({
    loading: true,
    err: "",
    limited: false,
    count: 0,
  });

  const lastLegend = useCallback(() => {
    const d = dataRef.current;
    return d.length ? { c: d[d.length - 1], prev: d[d.length - 2] } : null;
  }, []);

  const setAll = useCallback((rows: MainstreamCandle[]) => {
    dataRef.current = rows;
    byTs.current = new Map(rows.map((c, i) => [c.ts, i]));
    candleRef.current?.setData(rows.map(bar));
    volRef.current?.setData(rows.map(vol));
    if (rows.length) candleRef.current?.applyOptions({ priceFormat: priceFormat(rows[rows.length - 1].close) });
  }, []);

  // Chart instance: created once.
  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;
    const chart = createChart(host, {
      autoSize: true,
      layout: { background: { type: ColorType.Solid, color: "transparent" }, textColor: CHART_CHROME.text, fontSize: 11, fontFamily: CHART_CHROME.font },
      grid: { vertLines: { color: CHART_CHROME.grid }, horzLines: { color: CHART_CHROME.grid } },
      crosshair: { mode: CrosshairMode.Normal },
      rightPriceScale: { borderColor: CHART_CHROME.border, scaleMargins: { top: 0.08, bottom: 0.25 } },
      timeScale: { borderColor: CHART_CHROME.border, timeVisible: true, secondsVisible: false, rightOffset: 6 },
      // Pinch/wheel zoom and drag pan; vertical touch drags still scroll the page on phones.
      handleScroll: { mouseWheel: true, pressedMouseMove: true, horzTouchDrag: true, vertTouchDrag: false },
      handleScale: { mouseWheel: true, pinch: true, axisPressedMouseMove: true, axisDoubleClickReset: true },
      kineticScroll: { touch: true, mouse: false },
    });
    const candles = chart.addCandlestickSeries(candleColors());
    const volume = chart.addHistogramSeries({ priceFormat: { type: "volume" }, priceScaleId: "vol", lastValueVisible: false, priceLineVisible: false });
    chart.priceScale("vol").applyOptions({ scaleMargins: { top: 0.8, bottom: 0 } });
    chartRef.current = chart;
    candleRef.current = candles;
    volRef.current = volume;

    const onMove = (p: MouseEventParams) => {
      if (!p.time) {
        setLegend(lastLegend());
        return;
      }
      const i = byTs.current.get(fromTime(p.time));
      if (i == null) return;
      setLegend({ c: dataRef.current[i], prev: dataRef.current[i - 1] });
    };
    chart.subscribeCrosshairMove(onMove);
    // 红涨绿跌 toggle: canvas colours do not follow CSS, so re-apply them here.
    const offColors = onColorPref(() => {
      candles.applyOptions(candleColors());
      volume.setData(dataRef.current.map(vol));
    });
    return () => {
      offColors();
      chart.unsubscribeCrosshairMove(onMove);
      chart.remove();
      chartRef.current = null;
      candleRef.current = null;
      volRef.current = null;
    };
  }, [lastLegend]);

  // Load latest bars whenever the symbol/timeframe changes.
  useEffect(() => {
    const my = ++gen.current;
    exhausted.current = false;
    loadingOld.current = false;
    setAll([]);
    setLegend(null);
    setState({ loading: true, err: "", limited: false, count: 0 });
    marketProvider
      .getMainstreamCandles(symbol, tf, tf === "1d" ? 1000 : PAGE)
      .then((d) => {
        if (gen.current !== my) return;
        setAll(d.candles);
        const n = d.candles.length;
        chartRef.current?.timeScale().setVisibleLogicalRange({ from: Math.max(0, n - 140), to: n + 6 });
        setLegend(lastLegend());
        setState({ loading: false, err: d.candles.length ? "" : d.fetchError || "暂无K线数据", limited: !!d.limited, retention: d.retentionDays, count: n });
      })
      .catch((e: unknown) => gen.current === my && setState({ loading: false, err: e instanceof Error ? e.message : "读取失败", limited: false, count: 0 }));
  }, [symbol, tf, setAll, lastLegend]);

  // Scroll/zoom near the left edge pages older history in (bounded by the server's window).
  useEffect(() => {
    const chart = chartRef.current;
    if (!chart) return;
    const onRange = (r: LogicalRange | null) => {
      if (!r || r.from > 30 || loadingOld.current || exhausted.current || !dataRef.current.length) return;
      const my = gen.current;
      const oldest = dataRef.current[0].ts;
      loadingOld.current = true;
      marketProvider
        .getMainstreamCandles(symbol, tf, PAGE, oldest)
        .then((d) => {
          if (gen.current !== my) return;
          const older = d.candles.filter((c) => c.ts < oldest);
          if (!older.length || d.limited) exhausted.current = true;
          if (older.length) setAll([...older, ...dataRef.current]);
          setState((s) => ({ ...s, limited: exhausted.current, count: dataRef.current.length }));
        })
        .catch(() => undefined)
        .finally(() => {
          if (gen.current === my) loadingOld.current = false;
        });
    };
    chart.timeScale().subscribeVisibleLogicalRangeChange(onRange);
    return () => chart.timeScale().unsubscribeVisibleLogicalRangeChange(onRange);
  }, [symbol, tf, setAll]);

  // Keep the forming bar live.
  useEffect(() => {
    const my = gen.current;
    const t = window.setInterval(() => {
      if (document.hidden || !dataRef.current.length) return;
      marketProvider
        .getMainstreamCandles(symbol, tf, 3)
        .then((d) => {
          if (gen.current !== my) return;
          const last = dataRef.current[dataRef.current.length - 1];
          for (const c of d.candles) {
            if (c.ts < last.ts) continue;
            candleRef.current?.update(bar(c));
            volRef.current?.update(vol(c));
            const i = byTs.current.get(c.ts);
            if (i != null) dataRef.current[i] = c;
            else {
              byTs.current.set(c.ts, dataRef.current.length);
              dataRef.current.push(c);
            }
          }
          setLegend((l) => (l && l.c.ts < dataRef.current[dataRef.current.length - 2]?.ts ? l : lastLegend()));
        })
        .catch(() => undefined);
    }, POLL_MS[tf]);
    return () => window.clearInterval(t);
  }, [symbol, tf, lastLegend]);

  const lg = legend;
  const d = lg ? priceFormat(lg.c.close).precision : 2;
  const chg = lg?.prev ? lg.c.close / lg.prev.close - 1 : lg ? lg.c.close / lg.c.open - 1 : 0;
  const tone = chg >= 0 ? "up" : "down";

  return (
    <div className="msc">
      <div className="msc-legend num">
        {lg ? (
          <>
            <span className="muted">{fmtTs(lg.c.ts, tf)}</span>
            <span>开 <b className={tone}>{fmt(lg.c.open, d)}</b></span>
            <span>高 <b className={tone}>{fmt(lg.c.high, d)}</b></span>
            <span>低 <b className={tone}>{fmt(lg.c.low, d)}</b></span>
            <span>收 <b className={tone}>{fmt(lg.c.close, d)}</b></span>
            <span className={tone}>{chg >= 0 ? "+" : ""}{(chg * 100).toFixed(2)}%</span>
            <span>量 <b>{fmtVol(lg.c.volume)}</b></span>
          </>
        ) : (
          <span className="muted">{state.loading ? "加载中…" : state.err || "—"}</span>
        )}
      </div>
      <div className="msc-stage">
        <div ref={hostRef} className="msc-host" />
        {state.loading ? (
          <div className="msc-overlay msc-skel" aria-label="K线加载中">
            {Array.from({ length: 28 }, (_, i) => (
              <i key={i} style={{ height: `${22 + ((i * 37) % 50)}%`, marginTop: `${(i * 23) % 30}%` }} />
            ))}
          </div>
        ) : !state.count && state.err ? (
          <div className="msc-overlay msc-empty">
            <b>暂无 K 线</b>
            <span>{state.err}</span>
          </div>
        ) : null}
      </div>
      <div className="msc-foot muted">
        <span>
          {state.count} 根
          {INTRADAY.has(tf) ? ` · ${tf} 按需拉取，仅保留近 ${state.retention ?? 7} 天` : ""}
          {state.limited ? " · 已到最早可用数据" : ""}
        </span>
        <span className="msc-actions">
          <button type="button" onClick={() => chartRef.current?.timeScale().fitContent()}>全部</button>
          <button type="button" onClick={() => chartRef.current?.timeScale().scrollToRealTime()}>最新</button>
        </span>
      </div>
    </div>
  );
}
