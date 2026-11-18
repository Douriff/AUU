import type { UTCTimestamp } from "lightweight-charts";

/** One curve point. `epoch` changes after a sealed gap. */
export interface SegmentPoint {
  ts: number;
  epoch?: number;
}

export type SegmentLinePoint = { time: UTCTimestamp; value: number } | { time: UTCTimestamp };

/** Line points for one epoch at a time. A whitespace point keeps the chart from bridging a sealed gap. */
export function linePoints<T extends SegmentPoint>(points: T[], valueOf: (point: T) => number): SegmentLinePoint[] {
  const out: SegmentLinePoint[] = [];
  for (let i = 0; i < points.length; i++) {
    const point = points[i];
    const prev = points[i - 1];
    const epoch = point.epoch ?? 1;
    if (prev && epoch !== (prev.epoch ?? 1) && point.ts > prev.ts) {
      const breakAt = (Math.floor(prev.ts / 1000) + 86400) as UTCTimestamp;
      const here = Math.floor(point.ts / 1000) as UTCTimestamp;
      if (breakAt < here) out.push({ time: breakAt });
    }
    out.push({ time: Math.floor(point.ts / 1000) as UTCTimestamp, value: valueOf(point) });
  }
  return out;
}
