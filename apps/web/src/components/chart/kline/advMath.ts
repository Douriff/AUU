/** Pure geometry / number helpers for the advanced drawing tools (no KLineChart import, unit-tested: npm run test:adv). */

export type P = { x: number; y: number };

export const FIB_EXT = [0, 0.618, 1, 1.272, 1.618, 2, 2.618];
export const FIB_CHANNEL = [0, 0.236, 0.382, 0.5, 0.618, 0.786, 1, 1.618];

const EPS = 1e-9;
const FAR = 1e5;

/** End of the ray that starts at `q` with direction `d`, cut at the right (d.x > 0) or left (d.x < 0) edge of the pane. */
export function rayEnd(q: P, d: P, width: number): P {
  if (Math.abs(d.x) < EPS) return { x: q.x, y: q.y + (d.y >= 0 ? FAR : -FAR) };
  const X = d.x > 0 ? width : 0;
  const t = Math.max(0, (X - q.x) / d.x);
  return { x: q.x + d.x * t, y: q.y + d.y * t };
}

/** Andrews pitchfork: median line from P0 through the midpoint of P1-P2, plus parallels through P1 and P2. */
export function pitchfork(p0: P, p1: P, p2: P, width: number) {
  const m: P = { x: (p1.x + p2.x) / 2, y: (p1.y + p2.y) / 2 };
  const d: P = { x: m.x - p0.x, y: m.y - p0.y };
  const seg = (q: P): [P, P] => [q, rayEnd(q, d, width)];
  return { mid: m, dir: d, median: seg(p0), upper: seg(p1), lower: seg(p2), handle: [p1, p2] as [P, P] };
}

/** Trend-based fib extension: values project the A→B move from C. ratio 0 = C, 1 = same size as A→B. */
export function fibExtensionLevels(a: number, b: number, c: number, ratios: number[] = FIB_EXT): { ratio: number; value: number }[] {
  return ratios.map((ratio) => ({ ratio, value: c + (b - a) * ratio }));
}

/** Fib channel: base line P0→P1, width = vertical distance of P2 from that line; one parallel ray per ratio. */
export function channelLines(p0: P, p1: P, p2: P, width: number, ratios: number[] = FIB_CHANNEL): { ratio: number; from: P; to: P }[] {
  const d: P = { x: p1.x - p0.x, y: p1.y - p0.y };
  const off = Math.abs(d.x) < EPS ? 0 : p2.y - (p0.y + (d.y / d.x) * (p2.x - p0.x));
  return ratios.map((ratio) => {
    const from: P = { x: p0.x, y: p0.y + off * ratio };
    return { ratio, from, to: rayEnd(from, Math.abs(d.x) < EPS ? { x: 1, y: 0 } : d, width) };
  });
}

export type Measure = { delta: number; pct: number | null; bars: number; spanMs: number };

/** Price change, % change, bar count and time span between two points (value/time of point 1 relative to point 0). */
export function measureStats(v0: number, v1: number, t0: number, t1: number, barMs: number): Measure {
  const spanMs = Math.abs(t1 - t0);
  return {
    delta: v1 - v0,
    pct: Math.abs(v0) > EPS ? ((v1 - v0) / Math.abs(v0)) * 100 : null,
    bars: barMs > 0 ? Math.round(spanMs / barMs) : 0,
    spanMs,
  };
}

/** "2d 10h" style span (two largest non-zero units; "0m" for nothing). */
export function formatSpan(ms: number, u: { d: string; h: string; m: string } = { d: "d", h: "h", m: "m" }): string {
  const min = Math.floor(Math.max(0, ms) / 60_000);
  const d = Math.floor(min / 1440);
  const h = Math.floor((min % 1440) / 60);
  const m = min % 60;
  const parts: string[] = [];
  if (d) parts.push(`${d}${u.d}`);
  if (h) parts.push(`${h}${u.h}`);
  if (m && parts.length < 2) parts.push(`${m}${u.m}`);
  return parts.slice(0, 2).join(" ") || `0${u.m}`;
}

/** Typical bar length (ms) of a sorted bar list: the median gap of the last bars (robust to a missing candle). */
export function barMsOf(ts: number[]): number {
  const n = ts.length;
  if (n < 2) return 0;
  const gaps: number[] = [];
  for (let i = Math.max(1, n - 20); i < n; i++) gaps.push(ts[i] - ts[i - 1]);
  gaps.sort((a, b) => a - b);
  return gaps[Math.floor(gaps.length / 2)];
}
