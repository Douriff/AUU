import { useEffect, useMemo, useState } from "react";
import { marketProvider } from "@/providers/HttpWsProvider";
import type { MainstreamCandle, MainstreamFreshness, MainstreamItem, MainstreamOverview } from "@/types/mainstream";

const POLL_MS = 60_000;

function fmtPx(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n)) return "—";
  const digits = n >= 1000 ? 1 : n >= 10 ? 2 : 4;
  return n.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

function fmtPct(n: number | null | undefined, digits = 2): string {
  if (n == null || !Number.isFinite(n)) return "—";
  return `${n > 0 ? "+" : ""}${(n * 100).toFixed(digits)}%`;
}

function fmtRate(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n)) return "—";
  return `${n > 0 ? "+" : ""}${(n * 100).toFixed(4)}%`;
}

function tone(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n) || n === 0) return "flat";
  return n > 0 ? "up" : "down";
}

function fmtTime(ms: number | null | undefined): string {
  if (!ms) return "—";
  return new Date(ms).toLocaleString("zh-CN", { hour12: false, month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
}

function Spark({ values, w = 120, h = 32 }: { values: number[]; w?: number; h?: number }) {
  if (values.length < 2) return <svg width={w} height={h} aria-hidden="true" />;
  const lo = Math.min(...values);
  const hi = Math.max(...values);
  const span = hi - lo || 1;
  const pts = values.map((v, i) => `${((i / (values.length - 1)) * w).toFixed(1)},${(h - ((v - lo) / span) * (h - 2) - 1).toFixed(1)}`);
  const up = values[values.length - 1] >= values[0];
  return (
    <svg width={w} height={h} viewBox={`0 0 ${w} ${h}`} aria-hidden="true" className="ms-spark">
      <polyline points={pts.join(" ")} fill="none" stroke={up ? "#3ee08f" : "#ff5d5d"} strokeWidth="1.5" />
    </svg>
  );
}

function PriceChart({ candles }: { candles: MainstreamCandle[] }) {
  const w = 720;
  const h = 220;
  if (candles.length < 2) return <p className="mj-err">暂无K线数据。</p>;
  const closes = candles.map((c) => c.close);
  const lo = Math.min(...candles.map((c) => c.low));
  const hi = Math.max(...candles.map((c) => c.high));
  const span = hi - lo || 1;
  const y = (v: number) => h - ((v - lo) / span) * (h - 16) - 8;
  const x = (i: number) => (i / (candles.length - 1)) * w;
  const line = closes.map((v, i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
  return (
    <svg className="ms-chart" viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none" role="img" aria-label="收盘价走势">
      <polyline points={line} fill="none" stroke="#58a6ff" strokeWidth="1.6" vectorEffect="non-scaling-stroke" />
      <text x="4" y="12" className="ms-axis">{fmtPx(hi)}</text>
      <text x="4" y={h - 2} className="ms-axis">{fmtPx(lo)}</text>
    </svg>
  );
}

function FundingBars({ rows }: { rows: { ts: number; rate: number }[] }) {
  const w = 720;
  const h = 90;
  if (!rows.length) return <p className="mj-err">暂无资金费率数据。</p>;
  const m = Math.max(...rows.map((r) => Math.abs(r.rate)), 1e-6);
  const bw = w / rows.length;
  const mid = h / 2;
  return (
    <svg className="ms-chart ms-funding" viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none" role="img" aria-label="资金费率历史">
      <line x1="0" x2={w} y1={mid} y2={mid} stroke="rgba(255,255,255,0.15)" />
      {rows.map((r, i) => {
        const bh = (Math.abs(r.rate) / m) * (mid - 4);
        return (
          <rect
            key={r.ts}
            x={i * bw + 0.5}
            width={Math.max(bw - 1, 1)}
            y={r.rate >= 0 ? mid - bh : mid}
            height={Math.max(bh, 0.5)}
            fill={r.rate >= 0 ? "#3ee08f" : "#ff5d5d"}
          />
        );
      })}
    </svg>
  );
}

function FreshnessLine({ f }: { f: MainstreamFreshness }) {
  const blocked = Object.keys(f.blocked || {});
  return (
    <div className="ms-fresh">
      <span className={f.stale ? "is-down" : "is-ok"}>{f.stale ? "数据过期" : "数据新鲜"}</span>
      <span>来源 {f.exchange ? f.exchange.toUpperCase() : "—"}</span>
      <span>上次刷新 {fmtTime(f.lastRefreshMs)}</span>
      {blocked.length ? <span className="is-warn">不可用: {blocked.join(", ").toUpperCase()}</span> : null}
      {f.stale && f.staleSeries.length ? <span className="is-warn">过期: {f.staleSeries.join(", ")}</span> : null}
      {Object.keys(f.gaps || {}).length ? <span className="is-warn">缺口: {Object.entries(f.gaps).map(([k, v]) => `${k.split(":").slice(1).join(":")}×${v}`).join(", ")}</span> : null}
    </div>
  );
}

function Card({ item, active, onPick }: { item: MainstreamItem; active: boolean; onPick: () => void }) {
  const fr = item.fundingNow ?? item.fundingLast;
  return (
    <button type="button" className={`ms-card${active ? " is-on" : ""}`} onClick={onPick} aria-pressed={active}>
      <div className="ms-card-head">
        <b>{item.symbol}</b>
        <span className="muted">{item.pair}</span>
      </div>
      <div className="ms-price num">{fmtPx(item.price)}</div>
      <div className="ms-row">
        <span>24h <em className={tone(item.change24h)}>{fmtPct(item.change24h)}</em></span>
        <span>30d <em className={tone(item.change30d)}>{fmtPct(item.change30d)}</em></span>
      </div>
      <Spark values={item.spark1d || []} />
      <div className="ms-row">
        <span title="当前/最近一期资金费率（永续）">资金费率 <em className={tone(fr)}>{fmtRate(fr)}</em></span>
        <span title="按每 8 小时一期年化">年化 <em className={tone(item.fundingAnnualized)}>{fmtPct(item.fundingAnnualized, 1)}</em></span>
      </div>
      <div className="ms-row muted">
        <span>近期均值 {fmtRate(item.funding7dAvg)}</span>
        <span>下期 {fmtTime(item.nextFundingMs)}</span>
      </div>
    </button>
  );
}

/** 主流行情: BTC/ETH/SOL price + perpetual funding from public CEX data (read-only). */
export function MainstreamPage() {
  const [ov, setOv] = useState<MainstreamOverview | null>(null);
  const [err, setErr] = useState("");
  const [pick, setPick] = useState("");
  const [tf, setTf] = useState<"1d" | "1h">("1d");
  const [candles, setCandles] = useState<MainstreamCandle[]>([]);
  const [funding, setFunding] = useState<{ ts: number; rate: number }[]>([]);
  const [tick, setTick] = useState(0);

  useEffect(() => {
    let alive = true;
    marketProvider
      .getMainstreamOverview()
      .then((data) => {
        if (!alive) return;
        setOv(data);
        setErr("");
        setPick((p) => p || data.items[0]?.symbol || "");
      })
      .catch((e: unknown) => alive && setErr(e instanceof Error ? e.message : "读取失败"));
    const t = window.setTimeout(() => setTick((n) => n + 1), POLL_MS);
    return () => {
      alive = false;
      window.clearTimeout(t);
    };
  }, [tick]);

  useEffect(() => {
    if (!pick) return;
    let alive = true;
    void marketProvider
      .getMainstreamCandles(pick, tf, tf === "1d" ? 365 : 24 * 14)
      .then((d) => alive && setCandles(d.candles))
      .catch(() => alive && setCandles([]));
    void marketProvider
      .getMainstreamFunding(pick, 90)
      .then((d) => alive && setFunding(d.funding))
      .catch(() => alive && setFunding([]));
    return () => {
      alive = false;
    };
  }, [pick, tf, tick]);

  const current = useMemo(() => ov?.items.find((i) => i.symbol === pick), [ov, pick]);

  return (
    <div className="majors-page mj-dense mainstream-page">
      <header className="mj-top">
        <div>
          <h1>主流行情</h1>
          <p>交易所公开K线与永续资金费率（只读，无 API key，不下单）。纸面研究用。</p>
        </div>
        <div className="mj-health">
          <span className="mj-badge">LIVE OFF</span>
        </div>
      </header>
      {err ? <p className="mj-err">{err}</p> : null}
      {!ov && !err ? <p className="mj-err">正在读取行情…</p> : null}
      {ov ? <FreshnessLine f={ov.freshness} /> : null}
      {ov ? (
        <div className="ms-cards">
          {ov.items.map((item) => (
            <Card key={item.symbol} item={item} active={item.symbol === pick} onPick={() => setPick(item.symbol)} />
          ))}
        </div>
      ) : null}
      {current ? (
        <section className="ms-panel">
          <div className="mj-tools">
            <b>{current.pair}</b>
            <div className="mk-tabs" role="tablist" aria-label="周期">
              {(["1d", "1h"] as const).map((id) => (
                <button key={id} type="button" className={tf === id ? "is-on" : ""} onClick={() => setTf(id)}>
                  {id === "1d" ? "日线 1年" : "小时 14天"}
                </button>
              ))}
            </div>
          </div>
          <PriceChart candles={candles} />
          <div className="mj-tools ms-sub">
            <b>{current.perp} 资金费率</b>
            <span className="muted">最近 {funding.length} 期 · 绿=多头付费 红=空头付费</span>
          </div>
          <FundingBars rows={funding} />
        </section>
      ) : null}
    </div>
  );
}
