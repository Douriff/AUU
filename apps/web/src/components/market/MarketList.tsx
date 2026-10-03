import { useEffect, useMemo, useRef, useState } from "react";
import type { MarketRow } from "@/types/mainstream";
import { Num } from "@/components/ui/Num";
import { Empty } from "@/components/ui/Skeleton";

/** Exchange-style market list (own implementation): tabs, search, sortable columns, favorites. */

export type MarketTab = "fav" | "all" | "held";
type SortKey = "symbol" | "price" | "change24h" | "change7d" | "quoteVolume24h" | "funding" | "change30d";
type SortDir = "asc" | "desc";

const FAV_KEY = "auu.ms.favs";
const TAB_KEY = "auu.ms.tab";
const SORT_KEY = "auu.ms.sort";

export function fmtPx(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n)) return "—";
  const d = n >= 1000 ? 2 : n >= 100 ? 2 : n >= 1 ? 4 : n >= 0.01 ? 5 : Math.min(10, Math.max(7, Math.ceil(-Math.log10(Math.abs(n) || 1e-10)) + 3));
  return n.toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
}
export function fmtPct(n: number | null | undefined, digits = 2): string {
  if (n == null || !Number.isFinite(n)) return "—";
  return `${n > 0 ? "+" : ""}${(n * 100).toFixed(digits)}%`;
}
export function fmtRate(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n)) return "—";
  return `${n > 0 ? "+" : ""}${(n * 100).toFixed(4)}%`;
}
export function fmtVol(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n)) return "—";
  if (n >= 1e9) return `${(n / 1e9).toFixed(2)}B`;
  if (n >= 1e6) return `${(n / 1e6).toFixed(2)}M`;
  if (n >= 1e3) return `${(n / 1e3).toFixed(1)}K`;
  return n.toFixed(0);
}
export const tone = (n: number | null | undefined) => (n == null || !Number.isFinite(n) || n === 0 ? "flat" : n > 0 ? "up" : "down");

export function useFavorites(): [Set<string>, (s: string) => void] {
  const [favs, setFavs] = useState<Set<string>>(() => {
    try {
      const raw = JSON.parse(window.localStorage.getItem(FAV_KEY) || "null");
      return new Set(Array.isArray(raw) ? raw.filter((x) => typeof x === "string") : ["BTC", "ETH", "SOL"]);
    } catch {
      return new Set(["BTC", "ETH", "SOL"]);
    }
  });
  const toggle = (s: string) =>
    setFavs((prev) => {
      const next = new Set(prev);
      if (next.has(s)) next.delete(s);
      else next.add(s);
      window.localStorage.setItem(FAV_KEY, JSON.stringify([...next]));
      return next;
    });
  return [favs, toggle];
}

/** Integer / fraction widths (in ch) so a column of prices lines up on the decimal point. */
export function pxWidths(values: (number | null | undefined)[]): { int: number; frac: number } {
  let int = 1;
  let frac = 0;
  for (const v of values) {
    const t = fmtPx(v);
    const i = t.indexOf(".");
    int = Math.max(int, i < 0 ? t.length : i);
    frac = Math.max(frac, i < 0 ? 0 : t.length - i);
  }
  return { int, frac };
}

const COIN_HUES: Record<string, string> = { BTC: "#f7931a", ETH: "#627eea", SOL: "#9945ff", BNB: "#f3ba2f", XRP: "#23292f", DOGE: "#c2a633", ADA: "#0033ad", TRX: "#ef0027", LINK: "#2a5ada", AVAX: "#e84142", DOT: "#e6007a", LTC: "#345d9d", BCH: "#0ac18e", TON: "#0098ea" };
/** Round coin badge (letter mark; no third-party logos). */
export function CoinBadge({ symbol, size = 20 }: { symbol: string; size?: number }) {
  let h = 0;
  for (const ch of symbol) h = (h * 31 + ch.charCodeAt(0)) % 360;
  const bg = COIN_HUES[symbol] || `hsl(${h} 45% 38%)`;
  return (
    <span className="coin" style={{ width: size, height: size, background: bg, fontSize: Math.round(size * 0.5) }} aria-hidden="true">
      {symbol.slice(0, 1)}
    </span>
  );
}

export function Spark({ values, w = 96, h = 28 }: { values: number[]; w?: number; h?: number }) {
  if (!values || values.length < 2) return <svg width={w} height={h} aria-hidden="true" />;
  const lo = Math.min(...values);
  const hi = Math.max(...values);
  const span = hi - lo || 1;
  const pts = values.map((v, i) => `${((i / (values.length - 1)) * w).toFixed(1)},${(h - ((v - lo) / span) * (h - 2) - 1).toFixed(1)}`);
  const up = values[values.length - 1] >= values[0];
  return (
    <svg width={w} height={h} viewBox={`0 0 ${w} ${h}`} aria-hidden="true" className="ml-spark">
      <polyline points={pts.join(" ")} fill="none" className={up ? "spark-up" : "spark-down"} strokeWidth="1.4" />
    </svg>
  );
}

function sortRows(rows: MarketRow[], key: SortKey, dir: SortDir): MarketRow[] {
  const sign = dir === "asc" ? 1 : -1;
  return [...rows].sort((a, b) => {
    if (key === "symbol") return sign * a.symbol.localeCompare(b.symbol);
    const x = a[key] as number | null | undefined;
    const y = b[key] as number | null | undefined;
    if (x == null && y == null) return 0;
    if (x == null) return 1; // missing values always last
    if (y == null) return -1;
    return sign * (x - y);
  });
}

export function filterRows(rows: MarketRow[], q: string): MarketRow[] {
  const s = q.trim().toUpperCase();
  return s ? rows.filter((r) => r.symbol.includes(s) || r.pair.toUpperCase().includes(s)) : rows;
}

export function MarketList({ rows, onOpen, quote }: { rows: MarketRow[]; onOpen: (symbol: string) => void; quote: string }) {
  const [favs, toggleFav] = useFavorites();
  const [tab, setTabState] = useState<MarketTab>(() => (window.localStorage.getItem(TAB_KEY) as MarketTab) || "all");
  const [q, setQ] = useState("");
  const [sort, setSortState] = useState<{ key: SortKey; dir: SortDir }>(() => {
    try {
      const s = JSON.parse(window.localStorage.getItem(SORT_KEY) || "null");
      if (s && s.key && s.dir) return s;
    } catch {
      /* default */
    }
    return { key: "quoteVolume24h", dir: "desc" };
  });
  const setTab = (t: MarketTab) => {
    setTabState(t);
    window.localStorage.setItem(TAB_KEY, t);
  };
  const setSort = (key: SortKey) =>
    setSortState((prev) => {
      const next = { key, dir: (prev.key === key ? (prev.dir === "desc" ? "asc" : "desc") : key === "symbol" ? "asc" : "desc") as SortDir };
      window.localStorage.setItem(SORT_KEY, JSON.stringify(next));
      return next;
    });

  const counts = useMemo(() => ({ fav: rows.filter((r) => favs.has(r.symbol)).length, all: rows.length, held: rows.filter((r) => r.held).length }), [rows, favs]);
  const shown = useMemo(() => {
    const base = tab === "fav" ? rows.filter((r) => favs.has(r.symbol)) : tab === "held" ? rows.filter((r) => r.held) : rows;
    return sortRows(filterRows(base, q), sort.key, sort.dir);
  }, [rows, tab, favs, q, sort]);

  const pxW = useMemo(() => pxWidths(shown.map((r) => r.price)), [shown]);
  const Head = ({ k, label, className = "" }: { k: SortKey; label: string; className?: string }) => (
    <button type="button" className={`ml-h ${className}${sort.key === k ? " is-on" : ""}`} onClick={() => setSort(k)} aria-label={`按${label}排序`}>
      {label}
      <span className="ml-arrow">{sort.key === k ? (sort.dir === "desc" ? "↓" : "↑") : "↕"}</span>
    </button>
  );

  return (
    <section className="ml" aria-label="行情列表">
      <div className="ml-bar">
        <div className="ml-tabs" role="tablist" aria-label="列表">
          {([
            ["fav", "自选"],
            ["all", "全部"],
            ["held", "策略持仓"],
          ] as [MarketTab, string][]).map(([id, label]) => (
            <button key={id} type="button" role="tab" aria-selected={tab === id} className={tab === id ? "is-on" : ""} onClick={() => setTab(id)}>
              {label}
              <span className="ml-count">{counts[id]}</span>
            </button>
          ))}
        </div>
        <input className="ml-search" type="search" value={q} onChange={(e) => setQ(e.target.value)} placeholder="搜索币种" aria-label="搜索币种" />
      </div>
      <div className="ml-row ml-head" role="row">
        <span className="ml-c-star" />
        <Head k="symbol" label="币种" className="ml-c-name" />
        <Head k="price" label="最新价" className="ml-c-px" />
        <Head k="change24h" label="24h 涨跌" className="ml-c-chg" />
        <Head k="change7d" label="7 天" className="ml-c-7d" />
        <Head k="quoteVolume24h" label="24h 成交额" className="ml-c-vol" />
        <Head k="funding" label="资金费率" className="ml-c-fr" />
        <span className="ml-h ml-c-w">策略权重</span>
        <Head k="change30d" label="30 天" className="ml-c-spark" />
      </div>
      {shown.map((r) => (
        <div
          key={r.symbol}
          role="row"
          tabIndex={0}
          className="ml-row ml-item"
          onClick={() => onOpen(r.symbol)}
          onKeyDown={(e) => (e.key === "Enter" ? onOpen(r.symbol) : undefined)}
        >
          <button
            type="button"
            className={`ml-c-star ml-star${favs.has(r.symbol) ? " is-on" : ""}`}
            aria-label={favs.has(r.symbol) ? `取消自选 ${r.symbol}` : `加入自选 ${r.symbol}`}
            aria-pressed={favs.has(r.symbol)}
            onClick={(e) => {
              e.stopPropagation();
              toggleFav(r.symbol);
            }}
          >
            {favs.has(r.symbol) ? "★" : "☆"}
          </button>
          <span className="ml-c-name">
            <CoinBadge symbol={r.symbol} />
            <span className="ml-name-txt">
              <span className="ml-name-top">
                <b>{r.symbol}</b>
                <span className="ml-quote">/{quote}</span>
                {r.held ? (
                  <span className={`ml-held ${(r.weight ?? 0) < 0 ? "is-short" : ""}`} title="趋势策略纸面当前持仓">
                    {(r.weight ?? 0) < 0 ? "空" : "持仓"}
                  </span>
                ) : null}
              </span>
              <span className="ml-sub">成交额 {fmtVol(r.quoteVolume24h)}</span>
            </span>
          </span>
          <span className="ml-c-px num">
            <Num text={fmtPx(r.price)} int={pxW.int} frac={pxW.frac} />
            <span className="ml-sub">{fmtPct(r.change7d)} · 7天</span>
          </span>
          <span className="ml-c-chg">
            <em className={`ml-pill ${tone(r.change24h)}`}>{fmtPct(r.change24h)}</em>
          </span>
          <span className={`ml-c-7d num ${tone(r.change7d)}`}>{fmtPct(r.change7d)}</span>
          <span className="ml-c-vol num">{fmtVol(r.quoteVolume24h)}</span>
          <span className={`ml-c-fr num ${tone(r.funding)}`}>{fmtRate(r.funding)}</span>
          <span className="ml-c-w num">
            {r.held && r.weight != null ? (
              <span className="wbar" title={`${r.weight < 0 ? "空" : "多"} ${Math.abs(r.weight * 100).toFixed(1)}%`}>
                <span>
                  <i style={{ width: `${Math.min(100, Math.abs(r.weight) * 100 * 4)}%` }} />
                </span>
                {(r.weight * 100).toFixed(1)}%
              </span>
            ) : (
              <span className="dim">—</span>
            )}
          </span>
          <span className="ml-c-spark">
            <Spark values={r.spark30 || []} w={88} h={26} />
          </span>
        </div>
      ))}
      {!shown.length ? (
        <div className="ml-empty">
          {tab === "fav" && !q ? (
            <Empty icon="star" title="还没有自选" hint="在列表里点 ☆ 把常看的币加入自选" action={<button type="button" className="btn-ghost" onClick={() => setTab("all")}>查看全部</button>} />
          ) : tab === "held" && !q ? (
            <Empty icon="inbox" title="策略当前空仓" hint="趋势策略每日 08:00 调仓，信号触发后这里会列出持仓" />
          ) : (
            <Empty icon="search" title={`没有匹配「${q}」的币种`} hint="试试 BTC、ETH 或交易对名称" />
          )}
        </div>
      ) : null}
    </section>
  );
}

/** Quick coin switch for the chart page: button + searchable dropdown. */
export function CoinSwitcher({ rows, current, onPick, quote }: { rows: MarketRow[]; current: string; onPick: (s: string) => void; quote: string }) {
  const [open, setOpen] = useState(false);
  const [q, setQ] = useState("");
  const box = useRef<HTMLDivElement>(null);
  const input = useRef<HTMLInputElement>(null);
  const [favs] = useFavorites();
  useEffect(() => {
    if (!open) return;
    input.current?.focus();
    const off = (e: MouseEvent) => {
      if (box.current && !box.current.contains(e.target as Node)) setOpen(false);
    };
    window.addEventListener("mousedown", off);
    return () => window.removeEventListener("mousedown", off);
  }, [open]);
  const list = filterRows(rows, q).sort((a, b) => Number(favs.has(b.symbol)) - Number(favs.has(a.symbol)));
  return (
    <div className="cs" ref={box}>
      <button type="button" className="cs-btn" aria-haspopup="listbox" aria-expanded={open} onClick={() => setOpen((v) => !v)}>
        <CoinBadge symbol={current} size={22} />
        <b>{current}</b>
        <span className="cs-quote">/{quote}</span>
        <span className="cs-caret" aria-hidden="true">▾</span>
      </button>
      {open ? (
        <div className="cs-pop" role="listbox" aria-label="切换币种">
          <input
            ref={input}
            className="ml-search"
            type="search"
            value={q}
            onChange={(e) => setQ(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && list[0]) {
                onPick(list[0].symbol);
                setOpen(false);
                setQ("");
              }
              if (e.key === "Escape") setOpen(false);
            }}
            placeholder="搜索币种"
            aria-label="搜索币种"
          />
          <div className="cs-list">
            {list.map((r) => (
              <button
                key={r.symbol}
                type="button"
                role="option"
                aria-selected={r.symbol === current}
                className={`cs-item${r.symbol === current ? " is-on" : ""}`}
                onClick={() => {
                  onPick(r.symbol);
                  setOpen(false);
                  setQ("");
                }}
              >
                <span className="cs-name">
                  <CoinBadge symbol={r.symbol} size={16} />
                  <b>{r.symbol}</b>
                  {favs.has(r.symbol) ? <span className="cs-fav">★</span> : null}
                  {r.held ? <span className="ml-held">持仓</span> : null}
                </span>
                <span className="num">{fmtPx(r.price)}</span>
                <span className={tone(r.change24h)}>{fmtPct(r.change24h)}</span>
              </button>
            ))}
          </div>
        </div>
      ) : null}
    </div>
  );
}
