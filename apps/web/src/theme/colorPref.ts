import { useEffect, useState } from "react";

/**
 * Up/down colour preference. "green" = 绿涨红跌 (international default), "red" = 红涨绿跌 (CN).
 * CSS follows `html.cn` (see styles/pro.css); canvas charts read `chartColors()` and re-apply on change.
 */
export type ColorPref = "green" | "red";

const KEY = "auu.colors";
const EVENT = "auu:colors";

export function getColorPref(): ColorPref {
  try {
    return window.localStorage.getItem(KEY) === "red" ? "red" : "green";
  } catch {
    return "green";
  }
}

export function applyColorPref(p: ColorPref = getColorPref()): void {
  document.documentElement.classList.toggle("cn", p === "red");
}

export function setColorPref(p: ColorPref): void {
  try {
    window.localStorage.setItem(KEY, p);
  } catch {
    /* private mode: still apply for this tab */
  }
  applyColorPref(p);
  window.dispatchEvent(new CustomEvent(EVENT, { detail: p }));
}

export function onColorPref(fn: (p: ColorPref) => void): () => void {
  const h = () => fn(getColorPref());
  const s = (e: StorageEvent) => {
    if (e.key === KEY) {
      applyColorPref();
      h();
    }
  };
  window.addEventListener(EVENT, h);
  window.addEventListener("storage", s);
  return () => {
    window.removeEventListener(EVENT, h);
    window.removeEventListener("storage", s);
  };
}

export function useColorPref(): [ColorPref, (p: ColorPref) => void] {
  const [p, setP] = useState<ColorPref>(getColorPref);
  useEffect(() => onColorPref(setP), []);
  return [p, setColorPref];
}

const GREEN = "#1fb874";
const RED = "#f0474e";

export type ChartColors = { up: string; down: string; upVol: string; downVol: string };

export function chartColors(p: ColorPref = getColorPref()): ChartColors {
  const up = p === "red" ? RED : GREEN;
  const down = p === "red" ? GREEN : RED;
  const a = (hex: string, alpha: number) =>
    `rgba(${parseInt(hex.slice(1, 3), 16)},${parseInt(hex.slice(3, 5), 16)},${parseInt(hex.slice(5, 7), 16)},${alpha})`;
  return { up, down, upVol: a(up, 0.4), downVol: a(down, 0.4) };
}

/** Shared chrome for lightweight-charts instances (dark pro theme). */
export const CHART_CHROME = {
  text: "#7f8794",
  grid: "rgba(255,255,255,0.035)",
  border: "#1f242c",
  font: "'JetBrains Mono', ui-monospace, SFMono-Regular, Menlo, Consolas, monospace",
};
