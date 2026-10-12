/**
 * Pure TA helpers for lightweight-charts overlays/panes.
 * LWC v4 has no built-in indicators; we compute values and plot via addLineSeries / addHistogramSeries.
 */
import type { HistogramData, LineData, Time, UTCTimestamp } from "lightweight-charts";
import type { MainstreamCandle } from "@/types/mainstream";
import { chartColors } from "@/theme/colorPref";

export type Timed = { time: UTCTimestamp; value: number };

export function closesOf(rows: MainstreamCandle[], toTime: (ms: number) => UTCTimestamp): Timed[] {
  return rows.map((c) => ({ time: toTime(c.ts), value: c.close }));
}

export function sma(src: Timed[], period: number): LineData[] {
  const out: LineData[] = [];
  if (period < 1 || src.length < period) return out;
  let sum = 0;
  for (let i = 0; i < src.length; i++) {
    sum += src[i].value;
    if (i >= period) sum -= src[i - period].value;
    if (i >= period - 1) out.push({ time: src[i].time, value: sum / period });
  }
  return out;
}

export function ema(src: Timed[], period: number): LineData[] {
  const out: LineData[] = [];
  if (period < 1 || !src.length) return out;
  const k = 2 / (period + 1);
  let prev = src[0].value;
  for (let i = 0; i < src.length; i++) {
    prev = i === 0 ? src[0].value : src[i].value * k + prev * (1 - k);
    if (i >= period - 1) out.push({ time: src[i].time, value: prev });
  }
  return out;
}

export function bollinger(
  src: Timed[],
  period = 20,
  mult = 2,
): { mid: LineData[]; upper: LineData[]; lower: LineData[] } {
  const mid: LineData[] = [];
  const upper: LineData[] = [];
  const lower: LineData[] = [];
  if (src.length < period) return { mid, upper, lower };
  let sum = 0;
  let sumSq = 0;
  for (let i = 0; i < src.length; i++) {
    const v = src[i].value;
    sum += v;
    sumSq += v * v;
    if (i >= period) {
      const old = src[i - period].value;
      sum -= old;
      sumSq -= old * old;
    }
    if (i >= period - 1) {
      const mean = sum / period;
      const variance = Math.max(0, sumSq / period - mean * mean);
      const std = Math.sqrt(variance);
      const t = src[i].time;
      mid.push({ time: t, value: mean });
      upper.push({ time: t, value: mean + mult * std });
      lower.push({ time: t, value: mean - mult * std });
    }
  }
  return { mid, upper, lower };
}

export function macd(
  src: Timed[],
  fast = 12,
  slow = 26,
  signalPeriod = 9,
): { macd: LineData[]; signal: LineData[]; hist: HistogramData[] } {
  const macdLine: LineData[] = [];
  const signalLine: LineData[] = [];
  const hist: HistogramData[] = [];
  if (src.length < slow) return { macd: macdLine, signal: signalLine, hist };
  const kFast = 2 / (fast + 1);
  const kSlow = 2 / (slow + 1);
  const kSig = 2 / (signalPeriod + 1);
  let eFast = src[0].value;
  let eSlow = src[0].value;
  let eSig: number | null = null;
  const col = chartColors();
  for (let i = 0; i < src.length; i++) {
    const v = src[i].value;
    eFast = i === 0 ? v : v * kFast + eFast * (1 - kFast);
    eSlow = i === 0 ? v : v * kSlow + eSlow * (1 - kSlow);
    if (i < slow - 1) continue;
    const m = eFast - eSlow;
    macdLine.push({ time: src[i].time, value: m });
    eSig = eSig == null ? m : m * kSig + eSig * (1 - kSig);
    if (macdLine.length >= signalPeriod) {
      signalLine.push({ time: src[i].time, value: eSig });
      const h = m - eSig;
      hist.push({ time: src[i].time, value: h, color: h >= 0 ? col.upVol : col.downVol });
    }
  }
  return { macd: macdLine, signal: signalLine, hist };
}

/** Wilder RSI. */
export function rsi(src: Timed[], period = 14): LineData[] {
  const out: LineData[] = [];
  if (src.length <= period) return out;
  let avgGain = 0;
  let avgLoss = 0;
  for (let i = 1; i <= period; i++) {
    const d = src[i].value - src[i - 1].value;
    if (d >= 0) avgGain += d;
    else avgLoss -= d;
  }
  avgGain /= period;
  avgLoss /= period;
  const rs0 = avgLoss === 0 ? 100 : 100 - 100 / (1 + avgGain / avgLoss);
  out.push({ time: src[period].time, value: rs0 });
  for (let i = period + 1; i < src.length; i++) {
    const d = src[i].value - src[i - 1].value;
    const gain = d > 0 ? d : 0;
    const loss = d < 0 ? -d : 0;
    avgGain = (avgGain * (period - 1) + gain) / period;
    avgLoss = (avgLoss * (period - 1) + loss) / period;
    const value = avgLoss === 0 ? 100 : 100 - 100 / (1 + avgGain / avgLoss);
    out.push({ time: src[i].time, value });
  }
  return out;
}

/** Classic KDJ (n=9, m1=3, m2=3). */
export function kdj(
  rows: MainstreamCandle[],
  toTime: (ms: number) => UTCTimestamp,
  n = 9,
  m1 = 3,
  m2 = 3,
): { k: LineData[]; d: LineData[]; j: LineData[] } {
  const kArr: LineData[] = [];
  const dArr: LineData[] = [];
  const jArr: LineData[] = [];
  if (rows.length < n) return { k: kArr, d: dArr, j: jArr };
  let kPrev = 50;
  let dPrev = 50;
  for (let i = n - 1; i < rows.length; i++) {
    let hi = -Infinity;
    let lo = Infinity;
    for (let j = i - n + 1; j <= i; j++) {
      hi = Math.max(hi, rows[j].high);
      lo = Math.min(lo, rows[j].low);
    }
    const close = rows[i].close;
    const rsv = hi === lo ? 50 : ((close - lo) / (hi - lo)) * 100;
    const k = (kPrev * (m1 - 1) + rsv) / m1;
    const d = (dPrev * (m2 - 1) + k) / m2;
    const j = 3 * k - 2 * d;
    kPrev = k;
    dPrev = d;
    const t = toTime(rows[i].ts) as Time;
    kArr.push({ time: t, value: k });
    dArr.push({ time: t, value: d });
    jArr.push({ time: t, value: j });
  }
  return { k: kArr, d: dArr, j: jArr };
}

export const IND_COLORS = {
  ma7: "#f0b90b",
  ma25: "#e040fb",
  ma99: "#5b8def",
  ema12: "#26a69a",
  ema26: "#ab47bc",
  bollMid: "#787b86",
  bollBand: "#5b8def",
  macd: "#5b8def",
  macdSignal: "#f0b90b",
  rsi: "#bb86fc",
  k: "#f0b90b",
  d: "#5b8def",
  j: "#e91e63",
} as const;
