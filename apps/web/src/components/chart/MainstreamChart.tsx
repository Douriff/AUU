import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { Crosshair, KLineData, Period } from "klinecharts";
import { marketProvider } from "@/providers/HttpWsProvider";
import type { AuuSignalOverlay, MainstreamCandle, MainstreamTf, StrategyOverlay } from "@/types/mainstream";
import { chartColors, onColorPref } from "@/theme/colorPref";
import { useTranslation } from "react-i18next";
import i18n from "i18next";
import { errText } from "@/i18n/errors";
import { fmtDate, fmtFixed } from "@/i18n/format";
import { loadAuuRegimePref, loadAuuSignalPref, saveAuuRegimePref, saveAuuSignalPref } from "@/components/chart/auuSignalPrefs";
import { useKChart } from "@/components/chart/kline/useKChart";
import { useDrawings } from "@/components/chart/kline/useDrawings";
import { TOOL_POINTS } from "@/components/chart/kline/drawings";
import { useTouchScroll } from "@/components/chart/kline/useTouchScroll";
import { DrawToolbar } from "@/components/chart/kline/DrawToolbar";
import { IndicatorPanel } from "@/components/chart/kline/IndicatorPanel";
import { AlertPanel } from "@/components/chart/kline/AlertPanel";
import { usePriceAlerts } from "@/components/chart/kline/usePriceAlerts";
import { lineSpecs } from "@/components/chart/kline/priceAlerts";
import { MOM_COLORS, priceFormat } from "@/components/chart/kline/theme";
import { auuScoreIndicator, releaseIndicatorData, stratMomIndicator, type MarkerData } from "@/components/chart/kline/register";
import { regimeRuns, setMarkers, setRegimes } from "@/components/chart/kline/markers";
import type { CrosshairBus } from "@/components/chart/crosshairBus";
import { rtHub } from "@/realtime/hub";
import { QUICK_IDS, compactPrefs, layoutPanes, loadPrefs, savePrefs, syncIndicators, type IndicatorId, type IndicatorPrefs } from "@/components/chart/kline/indicatorPrefs";

/** KLineChart v10 (Apache-2.0) candlestick + volume + drawing tools for the mainstream page. */

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
// Forming-bar refresh. The server throttles its own exchange fetch of the last bar to once per ~10 s per series (and
// overlays the shared live ticker price), so polling at 10 s never adds exchange load.
const POLL_MS: Record<MainstreamTf, number> = { "1m": 10_000, "5m": 10_000, "15m": 10_000, "1h": 10_000, "4h": 10_000, "1d": 10_000 };
// While the forming bar is pushed (/api/v1/ws kline + ticker, 1h/4h/1d only) the poll above only re-calibrates
// every 30 s; it goes back to every 10 s as soon as the push is not live.
const POLL_LIVE_MS = 30_000;
const TF_MS: Partial<Record<MainstreamTf, number>> = { "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000 };
const PERIODS: Record<MainstreamTf, Period> = {
  "1m": { type: "minute", span: 1 },
  "5m": { type: "minute", span: 5 },
  "15m": { type: "minute", span: 15 },
  "1h": { type: "hour", span: 1 },
  "4h": { type: "hour", span: 4 },
  "1d": { type: "day", span: 1 },
};

const fmt = (n: number, d: number) => fmtFixed(n, d);
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
const toK = (c: MainstreamCandle): KLineData => ({ timestamp: c.ts, open: c.open, high: c.high, low: c.low, close: c.close, volume: c.volume });
const fromK = (k: KLineData): MainstreamCandle => ({ ts: k.timestamp, open: k.open, high: k.high, low: k.low, close: k.close, volume: k.volume ?? 0 });
const withAlpha = (hex: string, alpha: number) =>
  /^#[0-9a-f]{6}$/i.test(hex) ? `rgba(${parseInt(hex.slice(1, 3), 16)},${parseInt(hex.slice(3, 5), 16)},${parseInt(hex.slice(5, 7), 16)},${alpha})` : hex;
const fac = (v: number | null | undefined) => (v == null ? "—" : `${v > 0 ? "+" : ""}${fmtFixed(v * 100, 1)}`);
const pctTxt = (v: number | null | undefined, d = 1) => (v == null ? "—" : `${v > 0 ? "+" : ""}${fmtFixed(v * 100, d)}%`);

type OvRow = StrategyOverlay["series"][number];
type Legend = { c: MainstreamCandle; prev?: MainstreamCandle };
type Prefetch = { key: string; candles: MainstreamCandle[]; limited: boolean };

export function MainstreamChart({
  symbol,
  tf,
  venue = null,
  overlay = true,
  compact = false,
  bus = null,
  busId = "main",
}: {
  symbol: string;
  tf: MainstreamTf;
  venue?: string | null;
  overlay?: boolean;
  /** multi-chart cell: smaller footer, no per-cell credit line */
  compact?: boolean;
  /** synced crosshair: hovered bar time is shared with the other charts on this bus */
  bus?: CrosshairBus | null;
  busId?: string;
}) {
  const { t, i18n: i18nInst } = useTranslation();
  const lang = i18nInst.language;
  const hostRef = useRef<HTMLDivElement>(null);
  const chart = useKChart(hostRef);
  const drawings = useDrawings(chart, symbol);
  const getLast = useCallback(() => {
    const list = chart?.getDataList();
    const k = list && list.length ? list[list.length - 1] : null;
    return k ? { price: k.close, ts: k.timestamp } : null;
  }, [chart]);
  const alerts = usePriceAlerts(chart, symbol, getLast);
  const [alertOpen, setAlertOpen] = useState(false);
  const toggleAlerts = () => {
    if (alertOpen) {
      alerts.setPicking(false);
      alerts.clearFresh();
    } else {
      drawings.cancel();
      void alerts.openSeen();
    }
    setAlertOpen(!alertOpen);
  };
  useTouchScroll(hostRef, !!(drawings.active || drawings.selected || alerts.picking));

  const params = useRef({ symbol, tf, venue });
  const gen = useRef(0);
  const prefetch = useRef<Prefetch | null>(null);
  const poll = useRef<number | null>(null);
  const indIds = useRef(new Map<IndicatorId, { id: string; params: string }>());
  const [tick, setTick] = useState(0); // bumps when bars change (legend refresh)
  const [dataVer, setDataVer] = useState(0); // bumps when history was (pre)loaded: markers re-anchor
  const [hover, setHover] = useState<{ k: KLineData; prev?: KLineData } | null>(null);
  const [state, setState] = useState<{ loading: boolean; err: string; limited: boolean; retention?: number; count: number }>({ loading: true, err: "", limited: false, count: 0 });
  const [prefs, setPrefs] = useState<IndicatorPrefs>(() => (compact ? compactPrefs() : loadPrefs()));

  // strategy overlay (daily only): rebalance markers + TSMOM look-back return lines
  const [sigOn, setSigOn] = useState(false);
  const [ov, setOv] = useState<StrategyOverlay | null>(null);
  const [ovErr, setOvErr] = useState("");
  const ovRows = useMemo(() => new Map<number, OvRow>((ov?.series ?? []).map((r) => [r.ts, r])), [ov]);
  // AUU proprietary signal (experimental)
  const [auuOn, setAuuOn] = useState(() => (typeof window !== "undefined" ? loadAuuSignalPref() : false));
  const [auu, setAuu] = useState<AuuSignalOverlay | null>(null);
  const [auuErr, setAuuErr] = useState("");
  const auuRows = useMemo(() => new Map((auu?.series ?? []).map((r) => [r.asof_ts, r])), [auu]);
  const [auuRegime, setAuuRegime] = useState(() => (typeof window !== "undefined" ? loadAuuRegimePref() : true));
  // how long the latest long/short regime has run (bars) and since when
  const auuRun = useMemo(() => {
    const runs = regimeRuns(auu?.series ?? []);
    const last = runs[runs.length - 1];
    const lastTs = auu?.series[auu.series.length - 1]?.asof_ts;
    if (!last || last.to !== lastTs) return null;
    const bars = (auu?.series ?? []).filter((r) => r.asof_ts >= last.from).length;
    return { side: last.side, from: last.from, bars };
  }, [auu]);
  const [colorRev, setColorRev] = useState(0);
  useEffect(() => onColorPref(() => setColorRev((n) => n + 1)), []);

  params.current = { symbol, tf, venue };

  const rtOff = useRef<(() => void) | null>(null);
  const stopPoll = useCallback(() => {
    if (poll.current != null) window.clearInterval(poll.current);
    poll.current = null;
    rtOff.current?.();
    rtOff.current = null;
  }, []);

  // Data loader: first page comes from the prefetch below; "forward" pages older history in.
  useEffect(() => {
    if (!chart) return;
    chart.setDataLoader({
      getBars: async ({ type, timestamp, callback }) => {
        const P = params.current;
        const my = gen.current;
        try {
          if (type === "init") {
            const pf = prefetch.current;
            if (!pf || pf.key !== `${P.symbol}|${P.tf}|${P.venue ?? ""}`) return callback([], false);
            callback(pf.candles.map(toK), { forward: pf.candles.length > 0 && !pf.limited, backward: false });
            setTick((n) => n + 1);
            setDataVer((n) => n + 1);
          } else if (type === "forward" && timestamp != null) {
            // klinecharts v10: "forward" = older history, to the left of the first bar
            const d = await marketProvider.getMainstreamCandles(P.symbol, P.tf, PAGE, timestamp, P.venue);
            if (gen.current !== my) return callback([], { forward: false, backward: false });
            const older = d.candles.filter((c) => c.ts < timestamp);
            const done = !older.length || !!d.limited;
            callback(older.map(toK), { forward: !done, backward: false });
            setState((s) => ({ ...s, limited: done, count: s.count + older.length }));
            setDataVer((n) => n + 1);
          }
        } catch {
          callback([], false);
        }
      },
      // forming-bar refresh: poll our own API (same cadence as before)
      subscribeBar: ({ callback }) => {
        stopPoll();
        const P = params.current;
        const my = gen.current;
        // realtime push of the forming bar (only for the pool venue and 1h/4h/1d; otherwise nothing changes)
        const hub = rtHub();
        let lastPoll = 0;
        if (P.venue == null && TF_MS[P.tf]) {
          const step = TF_MS[P.tf]!;
          const offK = hub.subscribeKline(P.symbol, P.tf, (k) => {
            if (gen.current !== my || !hub.isLive()) return;
            const last = chart.getDataList().slice(-1)[0];
            if (!last || k.ts < last.timestamp) return;
            callback({ timestamp: k.ts, open: k.open, high: k.high, low: k.low, close: k.close, volume: k.volume });
            setTick((n) => n + 1);
          });
          // header and last bar from the same source (step 0): the 1 s ticker moves the forming bar's close too
          const offT = hub.subscribeTicker([P.symbol], (t) => {
            if (gen.current !== my || !hub.isLive()) return;
            const last = chart.getDataList().slice(-1)[0];
            const now = t.ts ?? t.recvAt;
            if (!last || now < last.timestamp || now >= last.timestamp + step) return;
            callback({ ...last, close: t.last, high: Math.max(last.high, t.last), low: Math.min(last.low, t.last) });
            setTick((n) => n + 1);
          });
          rtOff.current = () => {
            offK();
            offT();
          };
        }
        poll.current = window.setInterval(() => {
          if (document.hidden) return;
          if (hub.isLive() && rtOff.current && Date.now() - lastPoll < POLL_LIVE_MS) return;
          lastPoll = Date.now();
          marketProvider
            .getMainstreamCandles(P.symbol, P.tf, 3, undefined, P.venue)
            .then((d) => {
              if (gen.current !== my) return;
              const list = chart.getDataList();
              const last = list[list.length - 1];
              if (!last) return;
              for (const c of d.candles) if (c.ts >= last.timestamp) callback(toK(c));
              setTick((n) => n + 1);
            })
            .catch(() => undefined);
        }, POLL_MS[P.tf]);
      },
      unsubscribeBar: stopPoll,
    });
    return stopPoll;
  }, [chart, stopPoll]);

  // Load latest bars whenever the symbol / timeframe / venue changes.
  useEffect(() => {
    if (!chart) return;
    const my = ++gen.current;
    setState({ loading: true, err: "", limited: false, count: 0 });
    setHover(null);
    marketProvider
      .getMainstreamCandles(symbol, tf, tf === "1d" ? 1000 : PAGE, undefined, venue)
      .then((d) => {
        if (gen.current !== my) return;
        prefetch.current = { key: `${symbol}|${tf}|${venue ?? ""}`, candles: d.candles, limited: !!d.limited };
        const n = d.candles.length;
        setState({ loading: false, err: n ? "" : (i18n.language === "zh-CN" && d.fetchError) || i18n.t("chart.noData"), limited: !!d.limited, retention: d.retentionDays, count: n });
        const ticker = `${symbol}|${venue ?? ""}`;
        const precision = priceFormat(n ? d.candles[n - 1].close : 1).precision;
        const curSym = chart.getSymbol();
        const curPeriod = chart.getPeriod();
        const per = PERIODS[tf];
        const symChanged = !curSym || curSym.ticker !== ticker || curSym.pricePrecision !== precision;
        const perChanged = !curPeriod || curPeriod.type !== per.type || curPeriod.span !== per.span;
        if (symChanged) chart.setSymbol({ ticker, pricePrecision: precision, volumePrecision: 2 });
        if (perChanged) chart.setPeriod(per);
        if (!symChanged && !perChanged) chart.resetData();
      })
      .catch((e: unknown) => gen.current === my && setState({ loading: false, err: errText(e, "common.loadFailed"), limited: false, count: 0 }));
  }, [chart, symbol, tf, venue]);

  // Crosshair legend (+ shared hover time for synced multi-chart).
  const busRef = useRef({ bus, busId });
  busRef.current = { bus, busId };
  const [ghostX, setGhostX] = useState<number | null>(null);
  useEffect(() => {
    if (!chart || !bus) return setGhostX(null);
    return bus.on((src, ts) => {
      if (src === busRef.current.busId) return;
      if (ts == null) return setGhostX(null);
      const x = (chart.convertToPixel({ timestamp: ts }, { paneId: "candle_pane" }) as { x?: number }).x;
      const w = chart.getSize("candle_pane", "main")?.width ?? 0;
      setGhostX(x != null && Number.isFinite(x) && x >= 0 && x <= w ? x : null);
    });
  }, [chart, bus]);
  useEffect(() => {
    // pointer left the chart (mouse out / finger lifted): other charts drop their ghost line
    const el = hostRef.current;
    if (!chart || !el) return;
    const off = () => busRef.current.bus?.emit(busRef.current.busId, null);
    el.addEventListener("pointerleave", off);
    el.addEventListener("pointercancel", off);
    return () => {
      el.removeEventListener("pointerleave", off);
      el.removeEventListener("pointercancel", off);
    };
  }, [chart]);
  useEffect(() => {
    if (!chart) return;
    const onCross = (c: Crosshair) => {
      const k = c.kLineData;
      if (busRef.current.bus) {
        // the crosshair event carries kLineData only sometimes: fall back to the bar under x
        let ts: number | null = k ? k.timestamp : null;
        if (ts == null && c.x != null && c.paneId === "candle_pane") {
          const pt = chart.convertFromPixel([{ x: c.x }], { paneId: "candle_pane" }) as Array<{ timestamp?: number }> | { timestamp?: number };
          ts = (Array.isArray(pt) ? pt[0] : pt)?.timestamp ?? null;
        }
        busRef.current.bus.emit(busRef.current.busId, c.x == null ? null : ts);
      }
      if (!k) return setHover(null);
      const list = chart.getDataList();
      const i = c.dataIndex ?? c.realDataIndex ?? list.findIndex((x) => x.timestamp === k.timestamp);
      setHover({ k, prev: i != null && i > 0 ? list[i - 1] : undefined });
    };
    chart.subscribeAction("onCrosshairChange", onCross as never);
    return () => chart.unsubscribeAction("onCrosshairChange", onCross as never);
  }, [chart]);

  // Indicator toggles / parameters.
  useEffect(() => {
    if (!chart) return;
    syncIndicators(chart, prefs, indIds.current);
    layoutPanes(chart);
  }, [chart, prefs]);
  const changePrefs = (p: IndicatorPrefs) => {
    if (!compact) savePrefs(p); // multi-chart cells keep their own session-only indicators, the main chart's saved set is untouched
    setPrefs(p);
  };
  const toggleQuick = (id: IndicatorId) => changePrefs({ ...prefs, on: { ...prefs.on, [id]: !prefs.on[id] } });

  // Strategy overlay data: daily chart + toggle on. Read-only endpoint; refreshed when the symbol changes.
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

  const dropIndicator = useCallback((name: string) => {
    if (!chart) return;
    for (const ind of chart.getIndicators({ name })) releaseIndicatorData(ind.extendData);
    chart.removeIndicator({ name });
  }, [chart]);

  // Strategy overlay on the chart: TSMOM look-back lines (sub pane) + rebalance markers.
  useEffect(() => {
    if (!chart) return;
    dropIndicator("STRAT_MOM");
    if (!showSig || !ov) {
      setMarkers(chart, "strat", []);
      return;
    }
    const rows: Record<number, (number | null)[]> = {};
    for (const r of ov.series) rows[r.ts] = r.mom;
    chart.createIndicator(stratMomIndicator(rows, ov.lookbacks, (n) => t("chart.nDay", { n })), false);
    layoutPanes(chart);
    const col = chartColors();
    const markers: MarkerData[] = ov.fills.map((f) => ({
      ts: f.day,
      shape: "arrow",
      side: f.side === "buy" ? "low" : "high",
      color: f.side === "buy" ? col.up : col.down,
      text: `${f.side === "buy" ? t("chart.buy") : t("chart.sell")}→${fmtFixed(f.wTo * 100, 1)}%`,
    }));
    setMarkers(chart, "strat", markers);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [chart, ov, showSig, colorRev, lang, dataVer]);

  // AUU signal: score sub pane + enter/exit markers (experimental).
  useEffect(() => {
    if (!chart) return;
    dropIndicator("AUU_SCORE");
    if (!auuOn || !auu) {
      setMarkers(chart, "auu", []);
      return;
    }
    const rows: Record<number, number> = {};
    for (const r of auu.series) rows[r.asof_ts] = r.score * 100;
    const enterTh = typeof auu.params?.enter_th === "number" ? auu.params.enter_th * 100 : null;
    chart.createIndicator(auuScoreIndicator(rows, t("chart.auuScore"), enterTh != null ? { value: enterTh, title: t("chart.auuEnterTh") } : null), false);
    layoutPanes(chart);
    const col = chartColors();
    const markers: MarkerData[] = auu.series
      .filter((r) => r.action === "enter" || r.action === "exit")
      .map((r) => {
        const enter = r.action === "enter";
        const short = r.side === "short";
        return {
          ts: r.asof_ts,
          shape: enter ? "arrow" : "dot",
          side: enter && !short ? "low" : "high",
          color: enter ? (short ? col.down : col.up) : "rgba(255,255,255,0.7)",
          text: enter ? `${short ? t("chart.auuShort") : t("chart.auuLong")}·${t("chart.auuExpShort")}` : t("chart.auuExit"),
        };
      });
    setMarkers(chart, "auu", markers);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [chart, auu, auuOn, colorRev, lang, dataVer]);

  // AUU regime bands: translucent long/short background runs behind the candles.
  useEffect(() => {
    if (!chart) return;
    if (!auuOn || !auu || !auuRegime) {
      chart.removeOverlay({ groupId: "auuRegime" });
      return;
    }
    const col = chartColors();
    setRegimes(chart, regimeRuns(auu.series), { long: withAlpha(col.up, 0.09), short: withAlpha(col.down, 0.09) });
  }, [chart, auu, auuOn, auuRegime, colorRev, dataVer]);

  // keyboard: Delete removes the selected drawing, Esc cancels the tool / selection
  const onKeyDown = (e: React.KeyboardEvent) => {
    if ((e.target as HTMLElement).tagName === "INPUT") return;
    if (e.key === "Delete" || e.key === "Backspace") {
      if (drawings.selected) {
        e.preventDefault();
        drawings.removeSelected();
      }
    } else if (e.key === "Escape") drawings.cancel();
  };

  const bars = chart ? chart.getDataList() : [];
  void tick;
  const lastBar = bars.length ? { c: fromK(bars[bars.length - 1]), prev: bars.length > 1 ? fromK(bars[bars.length - 2]) : undefined } : null;
  const lg: Legend | null = hover ? { c: fromK(hover.k), prev: hover.prev ? fromK(hover.prev) : undefined } : lastBar;
  const d = lg ? priceFormat(lg.c.close).precision : 2;
  const chg = lg?.prev ? lg.c.close / lg.prev.close - 1 : lg ? lg.c.close / lg.c.open - 1 : 0;
  const tone = chg >= 0 ? "up" : "down";
  const hoverTs = lg?.c.ts;
  const ovRow = hoverTs != null ? ovRows.get(hoverTs) ?? null : null;
  const auuHover = hoverTs != null ? auuRows.get(hoverTs) ?? null : null;

  const fitAll = () => {
    if (!chart) return;
    const n = chart.getDataList().length;
    const w = chart.getSize("candle_pane", "main")?.width ?? 0;
    if (n && w) chart.setBarSpace(Math.max(1, (w - 60) / n));
  };

  // touch users: one-tap zoom around the latest bars (pinch still works)
  const zoomBy = (f: number) => {
    if (!chart) return;
    const w = chart.getSize("candle_pane", "main")?.width ?? 0;
    chart.zoomAtCoordinate(f, { x: Math.max(0, w - 60), y: 0 }, 120);
  };

  return (
    <div className={`msc${compact ? " msc-compact" : ""}`} tabIndex={-1} onKeyDown={onKeyDown}>
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
              {auuRun ? (
                <span title={t("chart.auuRunTitle")}>
                  {t("chart.auuRun")} <b className={auuRun.side === "long" ? "up" : "down"}>{t(`chart.auuSide_${auuRun.side}`)}</b>
                  <span className="muted"> · {t("chart.auuRunSince", { date: fmtDate(auuRun.from, { year: "numeric", month: "2-digit", day: "2-digit" }), n: auuRun.bars })}</span>
                </span>
              ) : null}
              {(() => {
                const row = auuHover ?? auu.latest ?? auu.series[auu.series.length - 1];
                if (!row) return <span className="muted">—</span>;
                return (
                  <>
                    <span>{t("chart.auuSide")} <b className={row.side === "long" ? "up" : row.side === "short" ? "down" : ""}>{t(`chart.auuSide_${row.side}`)}</b></span>
                    <span>{t("chart.auuScore")} <b>{fmtFixed(row.score * 100, 1)}</b></span>
                    <span>{t("chart.auuAction")} <b>{t(`chart.auuAction_${row.action}`)}</b></span>
                    {row.factors ? (
                      <span className="muted" title={t("chart.auuFactorsTitle")}>
                        {t("chart.auuFacMom")} {fac(row.factors.momentum)} · {t("chart.auuFacVol")} {fac(row.factors.volume_imbalance)} · {t("chart.auuFacFund")} {fac(row.factors.funding)}
                      </span>
                    ) : null}
                    {row.reason_tags?.length ? <span className="muted">{row.reason_tags.join(" · ")}</span> : null}
                  </>
                );
              })()}
            </>
          ) : (
            <span className="muted">{auuErr || t("chart.auuLoading")}</span>
          )}
        </div>
      ) : null}
      <div className="kc-body">
        <DrawToolbar
          active={drawings.active}
          hasSelection={!!drawings.selected}
          count={drawings.count}
          sync={drawings.syncState}
          magnet={drawings.magnet}
          onPick={drawings.pick}
          onMagnet={() => drawings.setMagnet((v) => !v)}
          onDelete={drawings.removeSelected}
          onClear={drawings.clearAll}
          alerts={{ open: alertOpen, unread: alerts.unread, active: lineSpecs(alerts.alerts, symbol).length, onToggle: toggleAlerts }}
        />
        {/* touch-action:none only while a tool is active / a drawing is selected, so page scroll still works otherwise */}
        <div className={`msc-stage${drawings.active || drawings.selected || alerts.picking ? " kc-touchlock" : ""}${alerts.picking ? " is-picking" : ""}`}>
          <div ref={hostRef} className="msc-host" />
          {ghostX != null ? <div className="kc-ghost" style={{ left: ghostX }} data-testid="kc-ghost" aria-hidden /> : null}
          {drawings.active ? <div className="kc-hint" data-testid="draw-hint">{TOOL_POINTS[drawings.active] ? t("chart.draw.hintN", { n: TOOL_POINTS[drawings.active] }) : t("chart.draw.hint")}</div> : null}
          {alerts.picking ? (
            <div className="kc-hint kc-hint-btn">
              {t("chart.alert.pickHint")}
              <button type="button" onClick={() => alerts.setPicking(false)}>{t("chart.draw.cancel")}</button>
            </div>
          ) : null}
          {alerts.toast ? (
            <div className="kc-alert-toast" role="status" data-testid="alert-toast">
              <span>
                🔔 {t(alerts.toast.firedDir === "up" ? "chart.alert.toastUp" : "chart.alert.toastDown", { sym: alerts.toast.symbol, p: fmt(alerts.toast.target, priceFormat(alerts.toast.target).precision) })}
              </span>
              <button type="button" onClick={alerts.dismissToast} aria-label={t("chart.alert.close")}>✕</button>
            </div>
          ) : null}
          {alertOpen ? (
            <AlertPanel
              symbol={symbol}
              last={lastBar ? lastBar.c.close : null}
              alerts={alerts.alerts}
              caps={alerts.caps}
              emailOn={alerts.emailOn}
              status={alerts.status}
              fresh={alerts.fresh}
              picking={alerts.picking}
              picked={alerts.picked}
              onPick={(on) => {
                if (on) drawings.cancel();
                alerts.setPicking(on);
              }}
              onPicked={alerts.clearPicked}
              onCreate={alerts.create}
              onRemove={alerts.remove}
              onClearHistory={alerts.clearHistory}
              onClose={toggleAlerts}
            />
          ) : null}
          {drawings.textEdit ? (
            <form
              className="kc-text-edit"
              onSubmit={(e) => {
                e.preventDefault();
                drawings.commitText(String(new FormData(e.currentTarget).get("t") ?? ""));
              }}
            >
              <input name="t" autoFocus maxLength={80} defaultValue={drawings.textEdit.text} placeholder={t("chart.draw.textPlaceholder")} aria-label={t("chart.draw.textPrompt")} />
              <button type="submit">{t("chart.draw.ok")}</button>
              <button type="button" onClick={() => drawings.commitText(drawings.textEdit?.text ?? "")}>{t("chart.draw.cancel")}</button>
            </form>
          ) : null}
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
      </div>
      <div className="msc-foot muted">
        <span>
          {t("chart.bars", { n: state.count })}
          {INTRADAY.has(tf) ? ` · ${t("chart.intraday", { tf, days: state.retention ?? 7 })}` : ""}
          {state.limited ? ` · ${t("chart.earliest")}` : ""}
        </span>
        <span className="msc-actions">
          <span className="msc-inds" role="group" aria-label={t("chart.ind.aria")}>
            {QUICK_IDS.map((id) => (
              <button key={id} type="button" className={prefs.on[id] ? "is-on" : ""} aria-pressed={prefs.on[id]} title={t(`chart.ind.${id}Title`)} data-ind-chip={id} onClick={() => toggleQuick(id)}>
                {t(`chart.ind.${id}`)}
              </button>
            ))}
            <IndicatorPanel prefs={prefs} onChange={changePrefs} />
          </span>
          {tf === "1d" && overlay ? (
            <button type="button" className={sigOn ? "is-on" : ""} aria-pressed={sigOn} title={t("chart.sigTitle")} data-testid="strat-sig-btn" onClick={() => setSigOn((v) => !v)}>
              {t("chart.sig")}
            </button>
          ) : null}
          <button
            type="button"
            className={auuOn ? "is-on" : ""}
            aria-pressed={auuOn}
            title={t("chart.auuTitle")}
            data-testid="auu-sig-btn"
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
          {auuOn ? (
            <button
              type="button"
              className={auuRegime ? "is-on" : ""}
              aria-pressed={auuRegime}
              title={t("chart.auuRegimeTitle")}
              data-testid="auu-regime-btn"
              onClick={() =>
                setAuuRegime((v) => {
                  saveAuuRegimePref(!v);
                  return !v;
                })
              }
            >
              {t("chart.auuRegime")}
            </button>
          ) : null}
          <span className="msc-zoom" role="group" aria-label={t("chart.zoom")}>
            <button type="button" aria-label={t("chart.zoomOut")} data-testid="zoom-out" onClick={() => zoomBy(0.8)}>−</button>
            <button type="button" aria-label={t("chart.zoomIn")} data-testid="zoom-in" onClick={() => zoomBy(1.25)}>＋</button>
          </span>
          <button type="button" data-testid="chart-all" onClick={fitAll}>{t("chart.all")}</button>
          <button type="button" onClick={() => chart?.scrollToRealTime()}>{t("chart.latest")}</button>
        </span>
        <span className="kc-credit">
          <a href="/THIRD_PARTY_NOTICES.txt" target="_blank" rel="noopener noreferrer">KLineChart · Apache-2.0</a>
        </span>
      </div>
    </div>
  );
}
