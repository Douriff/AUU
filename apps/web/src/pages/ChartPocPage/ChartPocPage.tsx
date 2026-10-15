/**
 * PROOF OF CONCEPT (not wired into nav, not deployed): KLineChart v10 (Apache-2.0) as a
 * TradingView-style chart. Route: /chart-poc
 * Shows: BTC candles from our own /api/v1/mainstream/candles, built-in drawing tools
 * (trend line, horizontal line, fib, rectangle, parallel channel), indicators (MA main pane,
 * MACD/RSI sub pane), drawings saved to localStorage (production: per-user server table),
 * and a demo "strategy signal" marker overlay (production: /api/v1/mainstream/signal).
 */
import { useEffect, useRef, useState } from "react";
import { dispose, init, registerOverlay, type Chart, type KLineData } from "klinecharts";

const TFS = [
  { id: "5m", span: 5, type: "minute" },
  { id: "15m", span: 15, type: "minute" },
  { id: "1h", span: 1, type: "hour" },
  { id: "4h", span: 4, type: "hour" },
  { id: "1d", span: 1, type: "day" },
] as const;

const TOOLS = [
  { name: "segment", label: "趋势线" },
  { name: "straightLine", label: "直线" },
  { name: "horizontalStraightLine", label: "水平线" },
  { name: "rayLine", label: "射线" },
  { name: "fibonacciLine", label: "斐波那契" },
  { name: "rect", label: "矩形" },
  { name: "parallelStraightLine", label: "平行通道" },
  { name: "simpleAnnotation", label: "标注" },
];

const STORE_KEY = "auu.poc.drawings.BTC";

// custom overlay = how our own strategy signals (long/exit arrows) would be drawn
registerOverlay({
  name: "auuSignal",
  totalStep: 1,
  lock: true,
  needDefaultPointFigure: false,
  createPointFigures: ({ coordinates, overlay }) => {
    const buy = (overlay.extendData as { side?: string } | undefined)?.side !== "exit";
    const { x, y } = coordinates[0];
    return [
      { type: "text", attrs: { x, y: buy ? y + 16 : y - 16, text: buy ? "▲ AUU long" : "▼ AUU exit", align: "center", baseline: "middle" },
        styles: { color: buy ? "#26a69a" : "#ef5350", size: 11, weight: "bold", backgroundColor: "transparent", borderSize: 0 } },
    ];
  },
});

async function fetchBars(tf: string, before?: number): Promise<KLineData[]> {
  const q = new URLSearchParams({ symbol: "BTC", tf, limit: "500" });
  if (before) q.set("before", String(before));
  const r = await fetch(`/api/v1/mainstream/candles?${q}`);
  const j = await r.json();
  const rows: { ts: number; open: number; high: number; low: number; close: number; volume: number }[] = j?.data?.candles ?? [];
  return rows.map((c) => ({ timestamp: c.ts, open: c.open, high: c.high, low: c.low, close: c.close, volume: c.volume }));
}

export function ChartPocPage() {
  const host = useRef<HTMLDivElement>(null);
  const chartRef = useRef<Chart | null>(null);
  const [tf, setTf] = useState<(typeof TFS)[number]["id"]>("1h");
  const [sub, setSub] = useState<"MACD" | "RSI" | "KDJ">("MACD");
  const [msg, setMsg] = useState("");
  const subId = useRef<string | null>(null);
  const pollRef = useRef<number | null>(null);

  useEffect(() => {
    const el = host.current!;
    const chart = init(el, { styles: "dark" })!;
    chartRef.current = chart;
    (window as unknown as { __c?: Chart }).__c = chart;
    chart.setStyles({
      grid: { horizontal: { color: "rgba(255,255,255,0.04)" }, vertical: { color: "rgba(255,255,255,0.04)" } },
      candle: { bar: { upColor: "#26a69a", downColor: "#ef5350", upBorderColor: "#26a69a", downBorderColor: "#ef5350", upWickColor: "#26a69a", downWickColor: "#ef5350" } },
    });
    chart.setTimezone("Asia/Shanghai");
    chart.setSymbol({ ticker: "BTC", pricePrecision: 2, volumePrecision: 2 });
    chart.createIndicator({ name: "MA", calcParams: [7, 25, 99], paneId: "candle_pane" }, true);
    chart.createIndicator("VOL");
    subId.current = chart.createIndicator("MACD") as string;
    const ro = new ResizeObserver(() => chart.resize());
    ro.observe(el);
    return () => {
      ro.disconnect();
      dispose(el);
    };
  }, []);

  useEffect(() => {
    const chart = chartRef.current;
    if (!chart) return;
    const p = TFS.find((t) => t.id === tf)!;
    chart.setDataLoader({
      // live updates: poll our API (production: reuse the existing /api/v1/ws feed)
      subscribeBar: ({ callback }) => {
        pollRef.current = window.setInterval(async () => {
          const d = await fetchBars(tf).catch(() => []);
          if (d.length) callback(d[d.length - 1]);
        }, 10_000);
      },
      unsubscribeBar: () => {
        if (pollRef.current) window.clearInterval(pollRef.current);
      },
      getBars: async ({ type, timestamp, callback }) => {
        try {
          if (type === "init") {
            const d = await fetchBars(tf);
            callback(d, { forward: d.length >= 500, backward: false });
            // demo strategy markers on a few swing lows/highs of the loaded window
            setTimeout(() => {
              chart.removeOverlay({ name: "auuSignal" });
              for (let i = 30; i < d.length - 5; i += 1) {
                const w = d.slice(i - 12, i + 13);
                if (d[i].low === Math.min(...w.map((b) => b.low))) chart.createOverlay({ name: "auuSignal", points: [{ timestamp: d[i].timestamp, value: d[i].low }], extendData: { side: "long" } });
                if (d[i].high === Math.max(...w.map((b) => b.high))) chart.createOverlay({ name: "auuSignal", points: [{ timestamp: d[i].timestamp, value: d[i].high }], extendData: { side: "exit" } });
              }
              try {
                const saved = JSON.parse(localStorage.getItem(`${STORE_KEY}.${tf}`) || "[]");
                for (const o of saved) chart.createOverlay(o);
              } catch { /* ignore */ }
            }, 50);
          } else if (type === "forward" && timestamp) {
            // klinecharts v10: "forward" = older history to the left of the first bar
            const d = (await fetchBars(tf, timestamp)).filter((b) => b.timestamp < timestamp);
            callback(d, { forward: d.length >= 400, backward: false });
          } else callback([], false);
        } catch (e) {
          setMsg(String(e));
          callback([], false);
        }
      },
    });
    chart.setPeriod({ span: p.span, type: p.type });
  }, [tf]);

  useEffect(() => {
    const chart = chartRef.current;
    if (!chart) return;
    if (subId.current) chart.removeIndicator({ id: subId.current });
    subId.current = chart.createIndicator(sub) as string;
  }, [sub]);

  const draw = (name: string) => chartRef.current?.createOverlay({ name, mode: "weak_magnet" as never });
  const save = () => {
    const list = (chartRef.current?.getOverlays() ?? []).filter((o) => o.name !== "auuSignal").map((o) => ({ name: o.name, points: o.points, styles: o.styles }));
    localStorage.setItem(`${STORE_KEY}.${tf}`, JSON.stringify(list));
    setMsg(`已保存 ${list.length} 个绘图（PoC 存浏览器；正式版存服务器按用户）`);
  };
  const clear = () => chartRef.current?.removeOverlay();

  const btn = { background: "#1b2230", color: "#cfd6e4", border: "1px solid #2b3447", borderRadius: 4, padding: "4px 10px", cursor: "pointer", fontSize: 12 } as const;
  return (
    <div style={{ background: "#0d1117", color: "#cfd6e4", padding: 12, display: "flex", flexDirection: "column", gap: 8 }}>
      <div style={{ fontWeight: 700 }}>BTC/USDT · KLineChart v10 PoC（Apache-2.0）</div>
      <div style={{ display: "flex", gap: 6, flexWrap: "wrap", alignItems: "center" }}>
        {TFS.map((t) => (
          <button key={t.id} style={{ ...btn, ...(tf === t.id ? { background: "#2962ff", borderColor: "#2962ff" } : {}) }} onClick={() => setTf(t.id)}>{t.id}</button>
        ))}
        <span style={{ width: 12 }} />
        {(["MACD", "RSI", "KDJ"] as const).map((s) => (
          <button key={s} style={{ ...btn, ...(sub === s ? { background: "#2962ff", borderColor: "#2962ff" } : {}) }} onClick={() => setSub(s)}>{s}</button>
        ))}
      </div>
      <div style={{ display: "flex", gap: 6, flexWrap: "wrap", alignItems: "center" }}>
        <span style={{ fontSize: 12, opacity: 0.7 }}>画线：</span>
        {TOOLS.map((t) => (
          <button key={t.name} style={btn} onClick={() => draw(t.name)}>{t.label}</button>
        ))}
        <button style={btn} onClick={save}>保存</button>
        <button style={btn} onClick={clear}>清除</button>
        <span style={{ fontSize: 12, opacity: 0.7 }}>{msg}</span>
      </div>
      <div ref={host} style={{ height: 620, border: "1px solid #1f242c" }} />
    </div>
  );
}
