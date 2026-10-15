import { useEffect, useState, type RefObject } from "react";
import { dispose, init, type Chart } from "klinecharts";
import { onColorPref } from "@/theme/colorPref";
import { useTranslation } from "react-i18next";
import { registerAuuKline } from "./register";
import { buildStyles } from "./theme";

export const kLocale = (lang: string) => (lang.startsWith("zh") ? "zh-CN" : "en-US");

/** Creates one KLineChart on `host`; keeps theme (up/down colours), locale and size in sync. */
export function useKChart(host: RefObject<HTMLDivElement>): Chart | null {
  const [chart, setChart] = useState<Chart | null>(null);
  const { i18n } = useTranslation();

  useEffect(() => {
    const el = host.current;
    if (!el) return;
    registerAuuKline();
    const c = init(el, {
      styles: "dark",
      locale: kLocale(i18n.language || "zh-CN"),
      timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC",
    });
    if (!c) return;
    c.setStyles(buildStyles());
    c.setOffsetRightDistance(48);
    setChart(c);
    if (import.meta.env.DEV) (window as unknown as { __c?: Chart }).__c = c;
    const ro = new ResizeObserver(() => c.resize());
    ro.observe(el);
    // 红涨绿跌 toggle: canvas colours do not follow CSS, re-apply them here
    const off = onColorPref(() => c.setStyles(buildStyles()));
    return () => {
      off();
      ro.disconnect();
      setChart(null);
      dispose(el);
    };
    // chart is created once per host
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [host]);

  useEffect(() => {
    chart?.setLocale(kLocale(i18n.language || "zh-CN"));
  }, [chart, i18n.language]);

  return chart;
}
