import type { Chart } from "klinecharts";
import type { MarkerData } from "./register";

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
