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
  type LineData,
  type MouseEventParams,
  type SeriesMarker,
  type Time,
  type UTCTimestamp,
} from "lightweight-charts";
import { marketProvider } from "@/providers/HttpWsProvider";
import type { MainstreamCandle, MainstreamTf, StrategyOverlay } from "@/types/mainstream";
import { CHART_CHROME, chartColors, onColorPref } from "@/theme/colorPref";
import { useTranslation } from "react-i18next";
import i18n from "i18next";
import { errText } from "@/i18n/errors";
import { fmtDate, fmtFixed, intlLocale } from "@/i18n/format";
import {
  IND_COLORS,
  bollinger,
  closesOf,
  ema,
  kdj,
  macd,
  rsi,
  sma,
} from "@/components/chart/indicators";
import {
  DEFAULT_INDICATOR_PREFS,
  INDICATOR_IDS,
  type IndicatorId,
  type IndicatorPrefs,
  loadIndicatorPrefs,
  saveIndicatorPrefs,
  toggleIndicator,
} from "@/components/chart/indicatorPrefs";
import { loadAuuSignalPref, saveAuuSignalPref } from "@/components/chart/auuSignalPrefs";
import type { AuuSignalOverlay, AuuSignalPoint } from "@/types/mainstream";

/** lightweight-charts (Apache-2.0) candlestick + volume for the mainstream page. */

/** label = i18n key */
export const CHART_TFS: { id: MainstreamTf; label: string }[] = [
  { id: "1m", label: "chart.tf.1m" },
  { id: "5m", label: "chart.tf.5m" },
  { id: "15m", label: "chart.tf.15m" },
  { id: "1h", label: "chart.tf.1h" },
  { id: "4h", label: "chart.tf.4h" },
  { id: "1d", label: "chart.tf.1d" },
];

const INTRADAY = new Set<MainstreamTf>(["1m", "5m", "15m"]);
const PAGE = 500;
const POLL_MS: Record<MainstreamTf, number> = { "1m": 10_000, "5m": 15_000, "15m": 20_000, "1h": 60_000, "4h": 60_000, "1d": 60_000 };

// The chart renders timestamps as UTC; shift by the browser offset so the axis shows local time.
const TZ_SHIFT_SEC = -new Date().getTimezoneOffset() * 60;
const toTime = (ms: number) => (Math.floor(ms / 1000) + TZ_SHIFT_SEC) as UTCTimestamp;
const fromTime = (t: Time) => ((t as number) - TZ_SHIFT_SEC) * 1000;

function priceFormat(px: number) {
  // sub-cent 大盘 coins (PEPE ~ 0.0000043) keep ~4 significant digits
  const precision = px >= 10 ? 2 : px >= 1 ? 3 : px >= 0.01 ? 5 : Math.min(10, Math.max(5, Math.ceil(-Math.log10(px || 1e-10)) + 3));
  return { type: "price" as const, precision, minMove: 10 ** -precision };
}

function fmt(n: number, d: number) {
  return fmtFixed(n, d);
}

function fmtVol(v: number) {
  if (v >= 1e9) return `${fmtFixed(v / 1e9, 2)}B`;
  if (v >= 1e6) return `${fmtFixed(v / 1e6, 2)}M`;
  if (v >= 1e3) return `${fmtFixed(v / 1e3, 2)}K`;
  return fmtFixed(v, 2);
}

function fmtTs(ms: number, tf: MainstreamTf) {
  const day = { year: "numeric", month: "2-digit", day: "2-digit" } as const;
  return tf === "1d" ? fmtDate(ms, day) : fmtDate(ms, { ...day, hour: "2-digit", minute: "2-digit", hour12: false });
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
type OvRow = StrategyOverlay["series"][number];

const MOM_COLORS = ["#f2c94c", "#bb86fc", "#4fc3f7"];
const pctTxt = (v: number | null | undefined, d = 1) => (v == null ? "—" : `${v > 0 ? "+" : ""}${fmtFixed(v * 100, d)}%`);

type LineSeries = ISeriesApi<"Line">;
type HistSeries = ISeriesApi<"Histogram">;

type IndSeriesBag = {
  ma: LineSeries[];
  ema: LineSeries[];
  boll: LineSeries[];
  macd: (LineSeries | HistSeries)[];
  rsi: LineSeries[];
  kdj: LineSeries[];
};

const emptyIndBag = (): IndSeriesBag => ({ ma: [], ema: [], boll: [], macd: [], rsi: [], kdj: [] });

function clearBag(chart: IChartApi, bag: IndSeriesBag) {
  for (const key of INDICATOR_IDS) {
    for (const s of bag[key]) {
      try {
        chart.removeSeries(s);
      } catch {
        /* already removed with chart */
      }
    }
    bag[key] = [];
  }
}

/** Stack volume / oscillator panes under the main candle scale. */
function applyPaneMargins(
  chart: IChartApi,
  opts: { showVol: boolean; panes: ("macd" | "rsi" | "kdj" | "auuSig")[]; showMom: boolean },
) {
  if (opts.showMom) {
    chart.priceScale("right").applyOptions({ scaleMargins: { top: 0.08, bottom: 0.25 } });
    return;
  }
  const paneH = 0.14;
  const volH = opts.showVol ? 0.1 : 0;
  const block = opts.panes.length * paneH + volH;
  chart.priceScale("right").applyOptions({
    scaleMargins: { top: 0.06, bottom: Math.min(0.55, block + (opts.panes.length || opts.showVol ? 0.02 : 0.22)) },
  });
  let bottom = 0;
  if (opts.showVol) {
    chart.priceScale("vol").applyOptions({ scaleMargins: { top: 1 - volH - bottom, bottom } });
    bottom += volH;
  }
  // panes from bottom up in reverse so first listed sits above volume
  for (let i = opts.panes.length - 1; i >= 0; i--) {
    const id = opts.panes[i];
    chart.priceScale(id).applyOptions({ scaleMargins: { top: 1 - paneH - bottom, bottom } });
    bottom += paneH;
  }
  if (!opts.panes.length && opts.showVol) {
    chart.priceScale("vol").applyOptions({ scaleMargins: { top: 0.8, bottom: 0 } });
    chart.priceScale("right").applyOptions({ scaleMargins: { top: 0.08, bottom: 0.25 } });
  }
  if (!opts.panes.length && !opts.showVol) {
    chart.priceScale("right").applyOptions({ scaleMargins: { top: 0.08, bottom: 0.08 } });
  }
}

export function MainstreamChart({ symbol, tf, venue = null, overlay = true }: { symbol: string; tf: MainstreamTf; venue?: string | null; overlay?: boolean }) {
  const { t, i18n: i18nInst } = useTranslation();
  const lang = i18nInst.language;
  const hostRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const candleRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const volRef = useRef<ISeriesApi<"Histogram"> | null>(null);
  const dataRef = useRef<MainstreamCandle[]>([]);
  const byTs = useRef<Map<number, number>>(new Map());
  const loadingOld = useRef(false);
  const exhausted = useRef(false);
  const gen = useRef(0);
  const indBag = useRef<IndSeriesBag>(emptyIndBag());
  const prefsRef = useRef<IndicatorPrefs>(DEFAULT_INDICATOR_PREFS);
  const syncMetaRef = useRef({ overlay: true as boolean, sigOn: false, hasOv: false, auuOn: false, hasAuu: false, tf: "1d" as MainstreamTf });
  const [legend, setLegend] = useState<Legend | null>(null);
  // strategy overlay (daily only): rebalance markers + TSMOM look-back return lines
  const [sigOn, setSigOn] = useState(false);
  const [ov, setOv] = useState<StrategyOverlay | null>(null);
  const [ovErr, setOvErr] = useState("");
  const ovRows = useRef<Map<number, OvRow>>(new Map());
  const momRefs = useRef<ISeriesApi<"Line">[]>([]);
  const [ovRow, setOvRow] = useState<OvRow | null>(null);
  // AUU proprietary signal (experimental) — separate from M3 strategy overlay + generic TA
  const [auuOn, setAuuOn] = useState(() => (typeof window !== "undefined" ? loadAuuSignalPref() : false));
  const [auu, setAuu] = useState<AuuSignalOverlay | null>(null);
  const [auuErr, setAuuErr] = useState("");
  const auuRows = useRef<Map<number, AuuSignalPoint>>(new Map());
  const auuScoreRef = useRef<ISeriesApi<"Line"> | null>(null);
  const [auuHover, setAuuHover] = useState<AuuSignalPoint | null>(null);
  const [indPrefs, setIndPrefs] = useState<IndicatorPrefs>(() =>
    typeof window !== "undefined" ? loadIndicatorPrefs() : { ...DEFAULT_INDICATOR_PREFS },
  );
  prefsRef.current = indPrefs;
  syncMetaRef.current = { overlay, sigOn, hasOv: !!ov, auuOn, hasAuu: !!auu, tf };
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

  const syncIndicators = useCallback(() => {
    const chart = chartRef.current;
    if (!chart) return;
    const rows = dataRef.current;
    const prefs = prefsRef.current;
    const meta = syncMetaRef.current;
    const showMom = meta.overlay && meta.sigOn && meta.tf === "1d" && meta.hasOv;
    clearBag(chart, indBag.current);

    // Strategy overlay claims the bottom band — skip oscillator panes while it is on.
    const allowPane = !showMom;
    const src = closesOf(rows, toTime);
    const lineOpts = (color: string, title: string, scaleId?: string) => ({
      color,
      lineWidth: 1 as const,
      lastValueVisible: false,
      priceLineVisible: false,
      title,
      ...(scaleId ? { priceScaleId: scaleId } : {}),
    });

    if (prefs.ma && rows.length) {
      const bag = indBag.current.ma;
      for (const [period, color] of [
        [7, IND_COLORS.ma7],
        [25, IND_COLORS.ma25],
        [99, IND_COLORS.ma99],
      ] as const) {
        const s = chart.addLineSeries(lineOpts(color, `MA${period}`));
        s.setData(sma(src, period));
        bag.push(s);
      }
    }
    if (prefs.ema && rows.length) {
      const bag = indBag.current.ema;
      for (const [period, color] of [
        [12, IND_COLORS.ema12],
        [26, IND_COLORS.ema26],
      ] as const) {
        const s = chart.addLineSeries(lineOpts(color, `EMA${period}`));
        s.setData(ema(src, period));
        bag.push(s);
      }
    }
    if (prefs.boll && rows.length) {
      const bag = indBag.current.boll;
      const { mid, upper, lower } = bollinger(src, 20, 2);
      const midS = chart.addLineSeries(lineOpts(IND_COLORS.bollMid, "BOLL"));
      const upS = chart.addLineSeries({ ...lineOpts(IND_COLORS.bollBand, "BOLL↑"), lineStyle: 2 });
      const loS = chart.addLineSeries({ ...lineOpts(IND_COLORS.bollBand, "BOLL↓"), lineStyle: 2 });
      midS.setData(mid);
      upS.setData(upper);
      loS.setData(lower);
      bag.push(midS, upS, loS);
    }

    const panes: ("macd" | "rsi" | "kdj" | "auuSig")[] = [];
    if (allowPane && prefs.macd && rows.length) {
      panes.push("macd");
      const bag = indBag.current.macd;
      const m = macd(src);
      const macdS = chart.addLineSeries(lineOpts(IND_COLORS.macd, "MACD", "macd"));
      const sigS = chart.addLineSeries(lineOpts(IND_COLORS.macdSignal, "Signal", "macd"));
      const histS = chart.addHistogramSeries({
        priceScaleId: "macd",
        lastValueVisible: false,
        priceLineVisible: false,
        priceFormat: { type: "price", precision: 4, minMove: 0.0001 },
      });
      macdS.setData(m.macd);
      sigS.setData(m.signal);
      histS.setData(m.hist);
      bag.push(macdS, sigS, histS);
    }
    if (allowPane && prefs.rsi && rows.length) {
      panes.push("rsi");
      const bag = indBag.current.rsi;
      const s = chart.addLineSeries({
        ...lineOpts(IND_COLORS.rsi, "RSI", "rsi"),
        priceFormat: { type: "price", precision: 2, minMove: 0.01 },
      });
      s.setData(rsi(src, 14));
      s.createPriceLine({ price: 70, color: "rgba(255,255,255,0.2)", lineWidth: 1, lineStyle: 2, axisLabelVisible: false, title: "" });
      s.createPriceLine({ price: 30, color: "rgba(255,255,255,0.2)", lineWidth: 1, lineStyle: 2, axisLabelVisible: false, title: "" });
      bag.push(s);
    }
    if (allowPane && prefs.kdj && rows.length) {
      panes.push("kdj");
      const bag = indBag.current.kdj;
      const kd = kdj(rows, toTime);
      const kS = chart.addLineSeries({ ...lineOpts(IND_COLORS.k, "K", "kdj"), priceFormat: { type: "price", precision: 2, minMove: 0.01 } });
      const dS = chart.addLineSeries({ ...lineOpts(IND_COLORS.d, "D", "kdj"), priceFormat: { type: "price", precision: 2, minMove: 0.01 } });
      const jS = chart.addLineSeries({ ...lineOpts(IND_COLORS.j, "J", "kdj"), priceFormat: { type: "price", precision: 2, minMove: 0.01 } });
      kS.setData(kd.k);
      dS.setData(kd.d);
      jS.setData(kd.j);
      bag.push(kS, dS, jS);
    }

    const showVol = !showMom;
    volRef.current?.applyOptions({ visible: showVol });
    const showAuuPane = !showMom && meta.auuOn && meta.hasAuu;
    const panesWithAuu: ("macd" | "rsi" | "kdj" | "auuSig")[] = showAuuPane ? [...panes, "auuSig"] : panes;
    applyPaneMargins(chart, { showVol, panes: panesWithAuu, showMom });
  }, []);

  const setAll = useCallback(
    (rows: MainstreamCandle[]) => {
      dataRef.current = rows;
      byTs.current = new Map(rows.map((c, i) => [c.ts, i]));
      candleRef.current?.setData(rows.map(bar));
      volRef.current?.setData(rows.map(vol));
      if (rows.length) candleRef.current?.applyOptions({ priceFormat: priceFormat(rows[rows.length - 1].close) });
      syncIndicators();
    },
    [syncIndicators],
  );

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
      localization: { locale: intlLocale() },
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
      setOvRow(ovRows.current.get(fromTime(p.time)) ?? null);
      setAuuHover(auuRows.current.get(fromTime(p.time)) ?? null);
      const i = byTs.current.get(fromTime(p.time));
      if (i == null) return;
      setLegend({ c: dataRef.current[i], prev: dataRef.current[i - 1] });
    };
    chart.subscribeCrosshairMove(onMove);
    // 红涨绿跌 toggle: canvas colours do not follow CSS, so re-apply them here.
    const offColors = onColorPref(() => {
      candles.applyOptions(candleColors());
      volume.setData(dataRef.current.map(vol));
      syncIndicators();
    });
    // Seed indicators if data already present (HMR / remount).
    syncIndicators();
    return () => {
      offColors();
      chart.unsubscribeCrosshairMove(onMove);
      clearBag(chart, indBag.current);
      chart.remove();
      chartRef.current = null;
      candleRef.current = null;
      volRef.current = null;
    };
    // syncIndicators identity changes with prefs/overlay; chart is created once — call via ref path on data updates.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [lastLegend]);

  // Axis / crosshair labels follow the interface language.
  useEffect(() => {
    chartRef.current?.applyOptions({ localization: { locale: intlLocale() } });
  }, [lang]);

  // Load latest bars whenever the symbol/timeframe changes.
  useEffect(() => {
    const my = ++gen.current;
    exhausted.current = false;
    loadingOld.current = false;
    setAll([]);
    setLegend(null);
    setState({ loading: true, err: "", limited: false, count: 0 });
    marketProvider
      .getMainstreamCandles(symbol, tf, tf === "1d" ? 1000 : PAGE, undefined, venue)
      .then((d) => {
        if (gen.current !== my) return;
        setAll(d.candles);
        const n = d.candles.length;
        chartRef.current?.timeScale().setVisibleLogicalRange({ from: Math.max(0, n - 140), to: n + 6 });
        setLegend(lastLegend());
        setState({ loading: false, err: d.candles.length ? "" : (i18n.language === "zh-CN" && d.fetchError) || i18n.t("chart.noData"), limited: !!d.limited, retention: d.retentionDays, count: n });
      })
      .catch((e: unknown) => gen.current === my && setState({ loading: false, err: errText(e, "common.loadFailed"), limited: false, count: 0 }));
  }, [symbol, tf, venue, setAll, lastLegend]);

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
        .getMainstreamCandles(symbol, tf, PAGE, oldest, venue)
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
  }, [symbol, tf, venue, setAll]);

  // Keep the forming bar live.
  useEffect(() => {
    const my = gen.current;
    const t = window.setInterval(() => {
      if (document.hidden || !dataRef.current.length) return;
      marketProvider
        .getMainstreamCandles(symbol, tf, 3, undefined, venue)
        .then((d) => {
          if (gen.current !== my) return;
          const last = dataRef.current[dataRef.current.length - 1];
          let changed = false;
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
            changed = true;
          }
          if (changed) syncIndicators();
          setLegend((l) => (l && l.c.ts < dataRef.current[dataRef.current.length - 2]?.ts ? l : lastLegend()));
        })
        .catch(() => undefined);
    }, POLL_MS[tf]);
    return () => window.clearInterval(t);
  }, [symbol, tf, venue, lastLegend, syncIndicators]);

  // Buy/sell markers follow the 红涨绿跌 toggle too.
  const [colorRev, setColorRev] = useState(0);
  useEffect(() => onColorPref(() => setColorRev((n) => n + 1)), []);

  // Overlay data: daily chart + toggle on. Read-only endpoint; refreshed when the symbol changes.
  const showSig = overlay && sigOn && tf === "1d";
  useEffect(() => {
    if (!showSig) {
      setOv(null);
      setOvErr("");
      return;
    }
    let live = true;
    marketProvider
      .getStrategyOverlay(symbol)
      .then((d) => live && (setOv(d), setOvErr(d.inUniverse ? "" : i18n.t("chart.notInPool"))))
      .catch((e: unknown) => live && (setOv(null), setOvErr(errText(e, "common.loadFailed"))));
    return () => {
      live = false;
    };
  }, [showSig, symbol]);

  // AUU proprietary signal (experimental): fetch whenever toggle is on.
  useEffect(() => {
    if (!auuOn) {
      setAuu(null);
      setAuuErr("");
      return;
    }
    let live = true;
    marketProvider
      .getAuuSignal(symbol, tf, tf === "1d" ? 500 : 400)
      .then((d) => live && (setAuu(d), setAuuErr("")))
      .catch((e: unknown) => live && (setAuu(null), setAuuErr(errText(e, "common.loadFailed"))));
    return () => {
      live = false;
    };
  }, [auuOn, symbol, tf]);

  useEffect(() => {
    const chart = chartRef.current;
    const candles = candleRef.current;
    if (!chart || !candles) return;
    for (const s of momRefs.current) chart.removeSeries(s);
    momRefs.current = [];
    ovRows.current = new Map();
    setOvRow(null);
    if (!showSig || !ov) {
      candles.setMarkers([]);
      syncIndicators();
      return;
    }
    ovRows.current = new Map(ov.series.map((r) => [r.ts, r]));
    momRefs.current = ov.lookbacks.map((L, k) => {
      const s = chart.addLineSeries({
        color: MOM_COLORS[k % MOM_COLORS.length],
        lineWidth: 1,
        priceScaleId: "mom",
        lastValueVisible: false,
        priceLineVisible: false,
        priceFormat: { type: "percent", precision: 1, minMove: 0.1 },
        title: i18n.t("chart.nDay", { n: L }),
      });
      s.setData(
        ov.series.filter((r) => r.mom[k] != null).map((r): LineData => ({ time: toTime(r.ts), value: (r.mom[k] as number) * 100 })),
      );
      if (k === 0) s.createPriceLine({ price: 0, color: "rgba(255,255,255,0.25)", lineWidth: 1, lineStyle: 2, axisLabelVisible: false, title: "" });
      return s;
    });
    chart.priceScale("mom").applyOptions({ scaleMargins: { top: 0.78, bottom: 0 } });
    const col = chartColors();
    const markers: SeriesMarker<Time>[] = ov.fills.map((f) => ({
      time: toTime(f.day),
      position: f.side === "buy" ? "belowBar" : "aboveBar",
      color: f.side === "buy" ? col.up : col.down,
      shape: f.side === "buy" ? "arrowUp" : "arrowDown",
      text: `${f.side === "buy" ? i18n.t("chart.buy") : i18n.t("chart.sell")}→${fmtFixed(f.wTo * 100, 1)}%`,
    }));
    candles.setMarkers(markers);
    syncIndicators();
  }, [ov, showSig, colorRev, lang, syncIndicators]);

  // AUU signal markers + soft score band (experimental). Markers only for enter/exit.
  useEffect(() => {
    const chart = chartRef.current;
    const candles = candleRef.current;
    if (!chart || !candles) return;
    if (auuScoreRef.current) {
      try {
        chart.removeSeries(auuScoreRef.current);
      } catch {
        /* gone */
      }
      auuScoreRef.current = null;
    }
    auuRows.current = new Map();
    setAuuHover(null);

    // When M3 strategy overlay owns markers, do not clobber them; AUU yields markers.
    const m3OwnsMarkers = showSig && !!ov;
    if (!auuOn || !auu) {
      if (!m3OwnsMarkers) candles.setMarkers([]);
      syncIndicators();
      return;
    }
    auuRows.current = new Map(auu.series.map((r) => [r.asof_ts, r]));
    const showMom = overlay && sigOn && tf === "1d" && !!ov;
    if (!showMom) {
      const scoreSeries = chart.addLineSeries({
        color: "rgba(242, 201, 76, 0.55)",
        lineWidth: 1,
        priceScaleId: "auuSig",
        lastValueVisible: false,
        priceLineVisible: false,
        priceFormat: { type: "price", precision: 2, minMove: 0.01 },
        title: t("chart.auuScore"),
      });
      scoreSeries.setData(
        auu.series.map((r): LineData => ({ time: toTime(r.asof_ts), value: r.score * 100 })),
      );
      scoreSeries.createPriceLine({ price: 0, color: "rgba(255,255,255,0.2)", lineWidth: 1, lineStyle: 2, axisLabelVisible: false, title: "" });
      scoreSeries.createPriceLine({ price: 25, color: "rgba(255,255,255,0.12)", lineWidth: 1, lineStyle: 2, axisLabelVisible: false, title: "" });
      scoreSeries.createPriceLine({ price: -25, color: "rgba(255,255,255,0.12)", lineWidth: 1, lineStyle: 2, axisLabelVisible: false, title: "" });
      auuScoreRef.current = scoreSeries;
    }
    if (!m3OwnsMarkers) {
      const col = chartColors();
      const markers: SeriesMarker<Time>[] = auu.series
        .filter((r) => r.action === "enter" || r.action === "exit")
        .map((r) => {
          const enter = r.action === "enter";
          return {
            time: toTime(r.asof_ts),
            position: enter ? (r.side === "short" ? "aboveBar" : "belowBar") : "aboveBar",
            color: enter ? (r.side === "short" ? col.down : col.up) : "rgba(255,255,255,0.55)",
            shape: enter ? (r.side === "short" ? "arrowDown" : "arrowUp") : "circle",
            text: enter
              ? `${r.side === "short" ? t("chart.auuShort") : t("chart.auuLong")}·${t("chart.auuExpShort")}`
              : t("chart.auuExit"),
          };
        });
      candles.setMarkers(markers);
    }
    syncIndicators();
  }, [auu, auuOn, showSig, ov, overlay, sigOn, tf, colorRev, lang, t, syncIndicators]);

  // Re-apply when user toggles indicator chips.
  useEffect(() => {
    syncIndicators();
  }, [indPrefs, syncIndicators]);

  const onToggleInd = (id: IndicatorId) => {
    setIndPrefs((prev) => {
      const next = toggleIndicator(prev, id);
      saveIndicatorPrefs(next);
      return next;
    });
  };

  const lg = legend;
  const d = lg ? priceFormat(lg.c.close).precision : 2;
  const chg = lg?.prev ? lg.c.close / lg.prev.close - 1 : lg ? lg.c.close / lg.c.open - 1 : 0;
  const tone = chg >= 0 ? "up" : "down";
  const paneBlocked = showSig && !!ov;

  return (
    <div className="msc">
      <div className="msc-legend num">
        {lg ? (
          <>
            <span className="muted">{fmtTs(lg.c.ts, tf)}</span>
            <span>{t("chart.o")} <b className={tone}>{fmt(lg.c.open, d)}</b></span>
            <span>{t("chart.h")} <b className={tone}>{fmt(lg.c.high, d)}</b></span>
            <span>{t("chart.l")} <b className={tone}>{fmt(lg.c.low, d)}</b></span>
            <span>{t("chart.c")} <b className={tone}>{fmt(lg.c.close, d)}</b></span>
            <span className={tone}>{chg >= 0 ? "+" : ""}{fmtFixed(chg * 100, 2)}%</span>
            <span>{t("chart.v")} <b>{fmtVol(lg.c.volume)}</b></span>
          </>
        ) : (
          <span className="muted">{state.loading ? t("common.loadingDots") : state.err || "—"}</span>
        )}
      </div>
      {showSig ? (
        <div className="msc-legend msc-sig num">
          {ov && ov.inUniverse ? (
            <>
              <span className="muted">{ov.strategy}</span>
              {ov.lookbacks.map((L, k) => (
                <span key={L} style={{ color: MOM_COLORS[k % MOM_COLORS.length] }}>
                  {t("chart.nDay", { n: L })} {pctTxt((ovRow ?? ov.series[ov.series.length - 1])?.mom[k])}
                </span>
              ))}
              {(() => {
                const row = ovRow ?? ov.series[ov.series.length - 1];
                const rec = row?.recorded;
                const differs = rec != null && Math.abs(rec - row.target) > 1e-9;
                return (
                  <>
                    <span>{t("chart.signal")} <b>{row?.signal != null ? fmtFixed(row.signal, 2) : "—"}</b></span>
                    <span>
                      {t("chart.target")} <b>{pctTxt(rec ?? row?.target, 2)}</b>
                      {rec != null ? <span className="muted">{t("chart.recorded")}</span> : null}
                      {differs ? <span className="bad"> · {t("chart.recalc", { v: pctTxt(row.target, 2) })}</span> : null}
                    </span>
                  </>
                );
              })()}
              <span className="muted">{t("chart.rebalances", { n: ov.fills.length })}</span>
              {ov.revised.length ? (
                <span className="bad" title={t("chart.revisedTitle")}>
                  {t("chart.revised", { n: ov.revised.length })}
                </span>
              ) : null}
            </>
          ) : (
            <span className="muted">{ovErr || t("chart.loadingSig")}</span>
          )}
        </div>
      ) : null}
      {auuOn ? (
        <div className="msc-legend msc-sig num">
          {auu ? (
            <>
              <span className="bad">{t("chart.auuExpBadge")}</span>
              <span className="muted">{auu.version}</span>
              {(() => {
                const row = auuHover ?? auu.latest ?? auu.series[auu.series.length - 1];
                if (!row) return <span className="muted">—</span>;
                return (
                  <>
                    <span>
                      {t("chart.auuSide")} <b>{row.side}</b>
                    </span>
                    <span>
                      {t("chart.auuScore")} <b>{fmtFixed(row.score * 100, 1)}</b>
                    </span>
                    <span>
                      {t("chart.auuAction")} <b>{row.action}</b>
                    </span>
                    {row.reason_tags?.length ? (
                      <span className="muted">{row.reason_tags.join(" · ")}</span>
                    ) : null}
                  </>
                );
              })()}
            </>
          ) : (
            <span className="muted">{auuErr || t("chart.auuLoading")}</span>
          )}
        </div>
      ) : null}
      <div className="msc-stage">
        <div ref={hostRef} className="msc-host" />
        {state.loading ? (
          <div className="msc-overlay msc-skel" aria-label={t("chart.loadingAria")}>
            {Array.from({ length: 28 }, (_, i) => (
              <i key={i} style={{ height: `${22 + ((i * 37) % 50)}%`, marginTop: `${(i * 23) % 30}%` }} />
            ))}
          </div>
        ) : !state.count && state.err ? (
          <div className="msc-overlay msc-empty">
            <b>{t("chart.empty")}</b>
            <span>{state.err}</span>
          </div>
        ) : null}
      </div>
      <div className="msc-foot muted">
        <span>
          {t("chart.bars", { n: state.count })}
          {INTRADAY.has(tf) ? ` · ${t("chart.intraday", { tf, days: state.retention ?? 7 })}` : ""}
          {state.limited ? ` · ${t("chart.earliest")}` : ""}
        </span>
        <span className="msc-actions">
          <span className="msc-inds" role="group" aria-label={t("chart.ind.aria")}>
            {INDICATOR_IDS.map((id) => {
              const isPane = id === "macd" || id === "rsi" || id === "kdj";
              const disabled = isPane && paneBlocked;
              return (
                <button
                  key={id}
                  type="button"
                  className={indPrefs[id] && !disabled ? "is-on" : ""}
                  aria-pressed={indPrefs[id] && !disabled}
                  disabled={disabled}
                  title={disabled ? t("chart.ind.paneBlocked") : t(`chart.ind.${id}Title`)}
                  onClick={() => onToggleInd(id)}
                >
                  {t(`chart.ind.${id}`)}
                </button>
              );
            })}
          </span>
          {tf === "1d" && overlay ? (
            <button
              type="button"
              className={sigOn ? "is-on" : ""}
              aria-pressed={sigOn}
              title={t("chart.sigTitle")}
              onClick={() => setSigOn((v) => !v)}
            >
              {t("chart.sig")}
            </button>
          ) : null}
          <button
            type="button"
            className={auuOn ? "is-on" : ""}
            aria-pressed={auuOn}
            title={t("chart.auuTitle")}
            onClick={() =>
              setAuuOn((v) => {
                const next = !v;
                saveAuuSignalPref(next);
                return next;
              })
            }
          >
            {t("chart.auuSig")}
          </button>
          <button type="button" onClick={() => chartRef.current?.timeScale().fitContent()}>{t("chart.all")}</button>
          <button type="button" onClick={() => chartRef.current?.timeScale().scrollToRealTime()}>{t("chart.latest")}</button>
        </span>
      </div>
    </div>
  );
}
