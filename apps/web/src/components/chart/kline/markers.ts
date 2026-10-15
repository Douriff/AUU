import type { Chart } from "klinecharts";
import type { MarkerData, RegimeData } from "./register";

/** Replace all marker overlays of one group (e.g. "strat" / "auu"). Anchors to the bar's high/low. */
export function setMarkers(chart: Chart, group: string, markers: MarkerData[]): void {
  chart.removeOverlay({ groupId: group });
  if (!markers.length) return;
  const data = chart.getDataList();
  if (!data.length) return;
  const first = data[0].timestamp;
  const list = [];
  for (const m of markers) {
    if (m.ts < first) continue; // older than the loaded history: appears once that page is loaded
    // snap to the bar that contains the timestamp (last bar with start <= ts)
    let lo = 0;
    let hi = data.length - 1;
    while (lo < hi) {
      const mid = (lo + hi + 1) >> 1;
      if (data[mid].timestamp <= m.ts) lo = mid;
      else hi = mid - 1;
    }
    const bar = data[lo];
    list.push({
      name: "auuMarker",
      groupId: group,
      lock: true,
      points: [{ timestamp: bar.timestamp, value: m.side === "low" ? bar.low : bar.high }],
      extendData: m,
    });
  }
  if (list.length) chart.createOverlay(list);
}

export type RegimeRun = { from: number; to: number; side: "long" | "short" };

/** Contiguous long/short runs of an AUU series (`to` = start of the last bar in the run). */
export function regimeRuns(series: { asof_ts: number; side: string }[]): RegimeRun[] {
  const runs: RegimeRun[] = [];
  let cur: RegimeRun | null = null;
  for (const r of series) {
    const side = r.side === "long" || r.side === "short" ? r.side : null;
    if (cur && side === cur.side) {
      cur.to = r.asof_ts;
      continue;
    }
    if (cur) runs.push(cur);
    cur = side ? { from: r.asof_ts, to: r.asof_ts, side } : null;
  }
  if (cur) runs.push(cur);
  return runs;
}

/** Replace the AUU regime bands (translucent, behind-the-action, never interactive). */
export function setRegimes(chart: Chart, runs: RegimeRun[], colors: { long: string; short: string }): void {
  chart.removeOverlay({ groupId: "auuRegime" });
  const data = chart.getDataList();
  if (!runs.length || data.length < 2) return;
  const first = data[0].timestamp;
  const barMs = Math.max(1, data[data.length - 1].timestamp - data[data.length - 2].timestamp);
  const list = [];
  for (const run of runs) {
    if (run.to < first) continue; // older than the loaded history
    const from = Math.max(run.from, first);
    list.push({
      name: "auuRegime",
      groupId: "auuRegime",
      lock: true,
      points: [
        { timestamp: from, value: 0 },
        { timestamp: run.to + barMs, value: 0 }, // bands span the whole bars of the run
      ],
      extendData: { color: run.side === "long" ? colors.long : colors.short } as RegimeData,
    });
  }
  if (list.length) chart.createOverlay(list);
}
