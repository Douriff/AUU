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
  const [legend, setLegend] = useState<Legend | null>(null);
  // strategy overlay (daily only): rebalance markers + TSMOM look-back return lines
  const [sigOn, setSigOn] = useState(false);
  const [ov, setOv] = useState<StrategyOverlay | null>(null);
  const [ovErr, setOvErr] = useState("");
  const ovRows = useRef<Map<number, OvRow>>(new Map());
  const momRefs = useRef<ISeriesApi<"Line">[]>([]);
  const [ovRow, setOvRow] = useState<OvRow | null>(null);
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
  }, [symbol, tf, venue, lastLegend]);

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
      volRef.current?.applyOptions({ visible: true });
      return;
    }
    volRef.current?.applyOptions({ visible: false }); // the bottom band shows the look-back returns instead
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
  }, [ov, showSig, colorRev, lang]);

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
          <button type="button" onClick={() => chartRef.current?.timeScale().fitContent()}>{t("chart.all")}</button>
          <button type="button" onClick={() => chartRef.current?.timeScale().scrollToRealTime()}>{t("chart.latest")}</button>
        </span>
      </div>
    </div>
  );
}
