import { useEffect, useRef } from "react";
import {
  createChart,
  type IChartApi,
  type ISeriesApi,
  type CandlestickData,
  type HistogramData,
  type Time,
  ColorType,
} from "lightweight-charts";
import type { Candle, Fill, SignalOut } from "@/types/contracts";

interface Props {
  candles: Candle[];
  signals: SignalOut[];
  fills: Fill[];
}

function toLwcTime(ms: number): Time {
  return Math.floor(ms / 1000) as Time;
}

export function CandleChart({ candles, signals, fills }: Props) {
  const hostRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const candleSeriesRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const volSeriesRef = useRef<ISeriesApi<"Histogram"> | null>(null);

  useEffect(() => {
    if (!hostRef.current) return;
    const chart = createChart(hostRef.current, {
      layout: {
        background: { type: ColorType.Solid, color: "#0d1117" },
        textColor: "#8b949e",
      },
      grid: {
        vertLines: { color: "#21262d" },
        horzLines: { color: "#21262d" },
      },
      crosshair: { mode: 1 },
      rightPriceScale: { borderColor: "#30363d" },
      timeScale: { borderColor: "#30363d", timeVisible: true, secondsVisible: false },
      width: hostRef.current.clientWidth,
      height: hostRef.current.clientHeight || 420,
    });
    const candleSeries = chart.addCandlestickSeries({
      upColor: "#3fb950",
      downColor: "#f85149",
      borderUpColor: "#3fb950",
      borderDownColor: "#f85149",
      wickUpColor: "#3fb950",
      wickDownColor: "#f85149",
    });
    const volSeries = chart.addHistogramSeries({
      priceFormat: { type: "volume" },
      priceScaleId: "vol",
    });
    chart.priceScale("vol").applyOptions({
      scaleMargins: { top: 0.8, bottom: 0 },
    });

    chartRef.current = chart;
    candleSeriesRef.current = candleSeries;
    volSeriesRef.current = volSeries;

    const ro = new ResizeObserver(() => {
      if (!hostRef.current) return;
      chart.applyOptions({
        width: hostRef.current.clientWidth,
        height: hostRef.current.clientHeight || 420,
      });
    });
    ro.observe(hostRef.current);

    return () => {
      ro.disconnect();
      chart.remove();
      chartRef.current = null;
      candleSeriesRef.current = null;
      volSeriesRef.current = null;
    };
  }, []);

  useEffect(() => {
    const series = candleSeriesRef.current;
    const vol = volSeriesRef.current;
    if (!series || !vol) return;

    const data: CandlestickData[] = candles.map((c) => ({
      time: toLwcTime(c.t),
      open: c.o,
      high: c.h,
      low: c.l,
      close: c.c,
    }));
    const vols: HistogramData[] = candles.map((c) => ({
      time: toLwcTime(c.t),
      value: c.v,
      color: c.c >= c.o ? "rgba(63,185,80,0.35)" : "rgba(248,81,73,0.35)",
    }));
    series.setData(data);
    vol.setData(vols);

    // Overlay markers: long→buy, short→sell; fills as diamonds
    const markers: {
      time: Time;
      position: "belowBar" | "aboveBar" | "inBar";
      color: string;
      shape: "arrowUp" | "arrowDown" | "circle" | "square";
      text: string;
      size?: number;
    }[] = [];

    for (const s of signals) {
      if (s.side === "flat" || s.t == null) continue;
      const strength = s.strength ?? 0.5;
      const size = Math.round(1 + strength * 2);
      if (s.side === "long") {
        markers.push({
          time: toLwcTime(s.t),
          position: "belowBar",
          color: "#3fb950",
          shape: "arrowUp",
          text: s.reason ? `L ${s.reason.slice(0, 16)}` : "LONG",
          size,
        });
      } else if (s.side === "short") {
        markers.push({
          time: toLwcTime(s.t),
          position: "aboveBar",
          color: "#f85149",
          shape: "arrowDown",
          text: s.reason ? `S ${s.reason.slice(0, 16)}` : "SHORT",
          size,
        });
      }
    }

    for (const f of fills) {
      markers.push({
        time: toLwcTime(f.ts),
        position: "inBar",
        color: f.qty >= 0 ? "#58a6ff" : "#d2a8ff",
        shape: "square", // closest to diamond in LWC markers
        text: `F ${f.qty > 0 ? "+" : ""}${f.qty.toFixed(0)}`,
        size: 1,
      });
    }

    markers.sort((a, b) => (a.time as number) - (b.time as number));
    series.setMarkers(markers);
    if (data.length) chartRef.current?.timeScale().scrollToRealTime();
  }, [candles, signals, fills]);

  return <div className="candle-chart" ref={hostRef} />;
}
