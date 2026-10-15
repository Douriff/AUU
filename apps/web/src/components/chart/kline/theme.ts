/** KLineChart v10 styling: dark pro theme, follows the up/down colour preference. */
import type { DeepPartial, Styles } from "klinecharts";
import { CHART_CHROME, chartColors } from "@/theme/colorPref";

export const IND_LINE_COLORS = ["#f0b90b", "#e040fb", "#5b8def", "#26a69a", "#ab47bc", "#e91e63"];
export const MOM_COLORS = ["#f2c94c", "#bb86fc", "#4fc3f7"];

export const isCoarsePointer = () => typeof window !== "undefined" && !!window.matchMedia?.("(pointer: coarse)").matches;

export function buildStyles(): DeepPartial<Styles> {
  const c = chartColors();
  const coarse = isCoarsePointer();
  const axisText = { color: CHART_CHROME.text, size: 11, family: CHART_CHROME.font };
  const tick = { show: true, color: CHART_CHROME.border };
  return {
    grid: { horizontal: { color: CHART_CHROME.grid }, vertical: { color: CHART_CHROME.grid } },
    candle: {
      bar: {
        compareRule: "current_open",
        upColor: c.up, downColor: c.down, noChangeColor: c.up,
        upBorderColor: c.up, downBorderColor: c.down, noChangeBorderColor: c.up,
        upWickColor: c.up, downWickColor: c.down, noChangeWickColor: c.up,
      },
      priceMark: { last: { upColor: c.up, downColor: c.down, noChangeColor: c.up } },
      // our own HTML legend renders OHLC (i18n + change %), so hide the built-in one
      tooltip: { showRule: "none" },
    },
    indicator: {
      ohlc: { compareRule: "current_open", upColor: c.up, downColor: c.down, noChangeColor: c.up },
      bars: [{ style: "fill", borderStyle: "solid", borderSize: 1, upColor: c.upVol, downColor: c.downVol, noChangeColor: c.upVol }],
      lines: IND_LINE_COLORS.map((color) => ({ style: "solid" as const, smooth: false, size: 1, dashedValue: [2, 2], color })),
      tooltip: { showRule: "always", showType: "standard", title: { color: CHART_CHROME.text } },
    },
    xAxis: { axisLine: { color: CHART_CHROME.border }, tickLine: tick, tickText: axisText },
    yAxis: { axisLine: { color: CHART_CHROME.border }, tickLine: tick, tickText: axisText },
    separator: { color: CHART_CHROME.border },
    crosshair: {
      horizontal: { line: { color: "rgba(255,255,255,0.28)" } },
      vertical: { line: { color: "rgba(255,255,255,0.28)" } },
    },
    overlay: {
      point: coarse
        ? { radius: 7, activeRadius: 10, borderSize: 1, activeBorderSize: 2 }
        : { radius: 4, activeRadius: 6, borderSize: 1, activeBorderSize: 2 },
      line: { color: "#4c9aff", size: 1.5 },
      polygon: { style: "stroke_fill", color: "rgba(76,154,255,0.14)", borderColor: "#4c9aff", borderSize: 1.5 },
      rect: { style: "stroke_fill", color: "rgba(76,154,255,0.14)", borderColor: "#4c9aff", borderSize: 1.5 },
      text: { color: "#e6edf3", size: 13, backgroundColor: "rgba(20,26,38,0.85)", borderColor: "#4c9aff", borderSize: 1 },
    },
  };
}

export function priceFormat(px: number) {
  // sub-cent 大盘 coins (PEPE ~ 0.0000043) keep ~4 significant digits
  const precision = px >= 10 ? 2 : px >= 1 ? 3 : px >= 0.01 ? 5 : Math.min(10, Math.max(5, Math.ceil(-Math.log10(px || 1e-10)) + 3));
  return { precision };
}
