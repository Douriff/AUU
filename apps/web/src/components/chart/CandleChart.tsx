import { useEffect, useRef } from "react";
import type { KLineData, Period } from "klinecharts";
import type { Candle, Fill, SignalOut } from "@/types/contracts";
import { chartColors, onColorPref } from "@/theme/colorPref";
import { useKChart } from "@/components/chart/kline/useKChart";
import { setMarkers } from "@/components/chart/kline/markers";
import { useTouchScroll } from "@/components/chart/kline/useTouchScroll";
import type { MarkerData } from "@/components/chart/kline/register";

interface Props {
  candles: Candle[];
  signals: SignalOut[];
  fills: Fill[];
}

const toK = (c: Candle): KLineData => ({ timestamp: c.t, open: c.o, high: c.h, low: c.l, close: c.c, volume: c.v });

/** Meme prices sit far below the default 0.01 tick, which pins the series to the axis. */
function precisionFor(prices: number[]): number {
  const abs = prices.map((n) => Math.abs(n)).filter((n) => Number.isFinite(n) && n > 0);
  if (!abs.length) return 4;
  const hi = Math.max(...abs);
  const lo = Math.min(...abs);
  const span = hi - lo;
  const unit = span > 0 ? span / 80 : hi / 1e4;
  return Math.min(12, Math.max(2, Math.ceil(-Math.log10(unit))));
}

/** Infer the bar size from the median spacing of the candles. */
function periodOf(candles: Candle[]): Period {
  const d: number[] = [];
  for (let i = 1; i < Math.min(candles.length, 60); i++) d.push(candles[i].t - candles[i - 1].t);
  d.sort((a, b) => a - b);
  const ms = d.length ? d[d.length >> 1] : 60_000;
  if (ms < 60_000) return { type: "second", span: Math.max(1, Math.round(ms / 1000)) };
  if (ms >= 86_400_000) return { type: "day", span: Math.max(1, Math.round(ms / 86_400_000)) };
  if (ms >= 3_600_000) return { type: "hour", span: Math.max(1, Math.round(ms / 3_600_000)) };
  return { type: "minute", span: Math.max(1, Math.round(ms / 60_000)) };
}

/** Compact K-line view (token pages): candles + volume + signal / fill markers. */
export function CandleChart({ candles, signals, fills }: Props) {
  const hostRef = useRef<HTMLDivElement>(null);
  const chart = useKChart(hostRef);
  useTouchScroll(hostRef, false);
  const candlesRef = useRef(candles);
  candlesRef.current = candles;
  const pushRef = useRef<((k: KLineData) => void) | null>(null);
  const loaded = useRef<{ first: number; last: number; n: number } | null>(null);

  useEffect(() => {
    if (!chart) return;
    chart.setDataLoader({
      getBars: ({ type, callback }) => {
        if (type !== "init") return callback([], false);
        const cs = candlesRef.current;
        callback(cs.map(toK), false);
        loaded.current = cs.length ? { first: cs[0].t, last: cs[cs.length - 1].t, n: cs.length } : null;
      },
      subscribeBar: ({ callback }) => {
        pushRef.current = callback;
      },
      unsubscribeBar: () => {
        pushRef.current = null;
      },
    });
    chart.createIndicator("VOL");
  }, [chart]);

  // Feed data: reset on structural change, otherwise push only the new / forming bars.
  useEffect(() => {
    if (!chart) return;
    if (!candles.length) {
      loaded.current = null;
      return;
    }
    const prev = loaded.current;
    const per = periodOf(candles);
    const precision = precisionFor(candles.flatMap((c) => [c.h, c.l, c.o, c.c]));
    const curSym = chart.getSymbol();
    const curPer = chart.getPeriod();
    const same = prev && prev.first === candles[0].t && candles.length >= prev.n && pushRef.current && curSym?.pricePrecision === precision && curPer?.type === per.type && curPer.span === per.span;
    if (same) {
      for (const c of candles) if (c.t >= prev.last) pushRef.current!(toK(c));
      loaded.current = { first: prev.first, last: candles[candles.length - 1].t, n: candles.length };
      return;
    }
    if (!curSym || curSym.pricePrecision !== precision) chart.setSymbol({ ticker: "token", pricePrecision: precision, volumePrecision: 0 });
    if (!curPer || curPer.type !== per.type || curPer.span !== per.span) chart.setPeriod(per);
    else chart.resetData();
  }, [chart, candles]);

  const buildMarkers = (): MarkerData[] => {
    const col = chartColors();
    const out: MarkerData[] = [];
    for (const s of signals) {
      if (s.side === "flat" || s.t == null) continue;
      const long = s.side === "long";
      out.push({ ts: s.t, shape: "arrow", side: long ? "low" : "high", color: long ? col.up : col.down, text: s.reason ? `${long ? "L" : "S"} ${s.reason.slice(0, 16)}` : long ? "LONG" : "SHORT" });
    }
    for (const f of fills) out.push({ ts: f.ts, shape: "dot", side: f.qty >= 0 ? "low" : "high", color: f.qty >= 0 ? "#58a6ff" : "#d2a8ff", text: `F ${f.qty > 0 ? "+" : ""}${f.qty.toFixed(0)}` });
    return out;
  };

  useEffect(() => {
    if (!chart) return;
    const apply = () => setMarkers(chart, "sig", buildMarkers());
    const t = window.setTimeout(apply, 60); // after the data (re)load settled
    const off = onColorPref(() => window.setTimeout(apply, 0));
    return () => {
      window.clearTimeout(t);
      off();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [chart, signals, fills, candles.length]);

  return <div className="candle-chart" ref={hostRef} />;
}
