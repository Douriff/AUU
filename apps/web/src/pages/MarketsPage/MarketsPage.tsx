import { memo, useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { PriceCell } from "@/components/markets/PriceCell";
import { Sparkline } from "@/components/markets/Sparkline";
import { useMarkets } from "@/hooks/useMarkets";
import type { MarketItem, MarketList } from "@/types/contracts";
import { truncateMint } from "@/venue";

const STAR_KEY = "auu:market-stars";

type Tab = "all" | "watch" | "gainers" | "losers" | "new";
type SortKey = "name" | "price" | "change" | "volume" | "mcap" | "progress";

const TABS: { id: Tab; label: string }[] = [
  { id: "all", label: "全部" },
  { id: "watch", label: "观察池" },
  { id: "gainers", label: "涨幅榜" },
  { id: "losers", label: "跌幅榜" },
  { id: "new", label: "新币" },
];

function readStars(): string[] {
  try {
    const raw = JSON.parse(localStorage.getItem(STAR_KEY) || "[]") as unknown;
    return Array.isArray(raw) ? raw.map(String) : [];
  } catch {
    return [];
  }
}

function formatPct(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n)) return "—";
  const pct = n * 100;
  const body = `${Math.abs(pct).toFixed(2)}%`;
  if (pct > 0) return `+${body}`;
  if (pct < 0) return `-${body}`;
  return body;
}

function formatSol(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n)) return "—";
  const a = Math.abs(n);
  if (a === 0) return "0";
  if (a >= 1e6) return `${(n / 1e6).toFixed(2)}M`;
  if (a >= 1e3) return `${(n / 1e3).toFixed(2)}K`;
  if (a >= 1) return n.toFixed(2);
  if (a >= 0.01) return n.toFixed(3);
  return n.toFixed(4);
}

function tone(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n) || n === 0) return "flat";
  return n > 0 ? "up" : "down";
}

function valueOf(row: MarketItem, key: SortKey): number | string | null {
  switch (key) {
    case "name":
      return row.base;
    case "price":
      return row.price_sol;
    case "change":
      return row.change_pct;
    case "volume":
      return row.volume_sol;
    case "mcap":
      return row.market_cap_sol;
    case "progress":
      return row.progress_bps;
  }
}

function compare(a: MarketItem, b: MarketItem, key: SortKey, dir: 1 | -1): number {
  const av = valueOf(a, key);
  const bv = valueOf(b, key);
  const aNil = av == null || av === "";
  const bNil = bv == null || bv === "";
  if (aNil && bNil) return a.symbol.localeCompare(b.symbol);
  if (aNil) return 1;
  if (bNil) return -1;
  if (typeof av === "string" && typeof bv === "string") return av.localeCompare(bv) * dir;
  return ((av as number) - (bv as number)) * dir;
}

const MarketRow = memo(function MarketRow({
  row,
  rank,
  starred,
  onToggle,
  onOpen,
}: {
  row: MarketItem;
  rank: number;
  starred: boolean;
  onToggle: (symbol: string) => void;
  onOpen: (row: MarketItem) => void;
}) {
  const progress = row.progress_pct;
  return (
    <tr className="mk-row" onClick={() => onOpen(row)}>
      <td className="mk-star">
        <button
          type="button"
          className={starred ? "is-on" : ""}
          aria-pressed={starred}
          aria-label={starred ? `取消观察 ${row.base}` : `加入观察池 ${row.base}`}
          onClick={(event) => {
            event.stopPropagation();
            onToggle(row.symbol);
          }}
        >
          {starred ? "★" : "☆"}
        </button>
      </td>
      <td className="mk-rank">{rank}</td>
      <td className="mk-name">
        <strong>{row.base}</strong>
        <em>{row.mint ? truncateMint(row.mint, 4, 4) : row.symbol}</em>
      </td>
      <td className="mk-num">
        <PriceCell price={row.price_sol} />
      </td>
      <td className={`mk-num ${tone(row.change_pct)}`}>{formatPct(row.change_pct)}</td>
      <td className="mk-num">{formatSol(row.volume_sol)}</td>
      <td className="mk-num">{formatSol(row.market_cap_sol)}</td>
      <td className="mk-num mk-progress">
        {progress == null ? (
          "—"
        ) : (
          <span className="mk-prog">
            <span className="mk-prog-track">
              <i style={{ width: `${Math.max(0, Math.min(100, progress))}%` }} />
            </span>
            <b>{progress.toFixed(1)}%</b>
          </span>
        )}
      </td>
      <td className="mk-spark-cell">
        <Sparkline points={row.spark} />
      </td>
    </tr>
  );
});

function HeaderButton({
  label,
  sortKey,
  active,
  dir,
  onSort,
}: {
  label: string;
  sortKey: SortKey;
  active: SortKey;
  dir: 1 | -1;
  onSort: (key: SortKey) => void;
}) {
  const on = active === sortKey;
  return (
    <button type="button" onClick={() => onSort(sortKey)} aria-sort={on ? (dir < 0 ? "descending" : "ascending") : "none"}>
      {label}
      <span aria-hidden="true">{on ? (dir < 0 ? " ↓" : " ↑") : ""}</span>
    </button>
  );
}

export function MarketsPage() {
  const navigate = useNavigate();
  const { list, err } = useMarkets(2500);
  const [tab, setTab] = useState<Tab>("all");
  const [query, setQuery] = useState("");
  const [sortKey, setSortKey] = useState<SortKey>("mcap");
  const [sortDir, setSortDir] = useState<1 | -1>(-1);
  const [stars, setStars] = useState<string[]>(() => readStars());

  useEffect(() => {
    localStorage.setItem(STAR_KEY, JSON.stringify(stars));
  }, [stars]);

  const starSet = useMemo(() => new Set(stars), [stars]);

  const rows = useMemo(() => {
    const items = list?.items ?? [];
    const q = query.trim().toLowerCase();
    let next = items.filter((item) => {
      if (tab === "watch" && !starSet.has(item.symbol)) return false;
      if (tab === "new" && !item.discovered) return false;
      if (!q) return true;
      return (
        item.base.toLowerCase().includes(q) ||
        item.symbol.toLowerCase().includes(q) ||
        item.mint.toLowerCase().includes(q)
      );
    });
    const key = tab === "gainers" || tab === "losers" ? "change" : sortKey;
    const dir = tab === "gainers" ? -1 : tab === "losers" ? 1 : sortDir;
    next = [...next].sort((a, b) => compare(a, b, key, dir));
    return next;
  }, [list, query, tab, sortKey, sortDir, starSet]);

  const onSort = (key: SortKey) => {
    if (key === sortKey) setSortDir((d) => (d < 0 ? 1 : -1));
    else {
      setSortKey(key);
      setSortDir(key === "name" ? 1 : -1);
    }
    if (tab === "gainers" || tab === "losers") setTab("all");
  };

  const toggleStar = (symbol: string) => {
    setStars((prev) => (prev.includes(symbol) ? prev.filter((s) => s !== symbol) : [...prev, symbol]));
  };

  return (
    <div className="markets-page">
      <header className="mk-head">
        <div>
          <h1>市场</h1>
          <p className="mk-note">{noteFor(list, err)}</p>
        </div>
        <label className="mk-search">
          <span className="sr-only">搜索</span>
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="搜索名称 / 符号 / mint"
            type="search"
          />
        </label>
      </header>

      <div className="mk-tabs" role="tablist" aria-label="市场筛选">
        {TABS.map((item) => (
          <button
            key={item.id}
            type="button"
            role="tab"
            aria-selected={tab === item.id}
            className={tab === item.id ? "is-on" : ""}
            onClick={() => setTab(item.id)}
          >
            {item.label}
          </button>
        ))}
      </div>

      {!list && !err ? <p className="mk-empty">读取市场…</p> : null}
      {list ? (
        <div className="mk-table-wrap">
          <table className="mk-table">
            <thead>
              <tr>
                <th aria-label="观察" />
                <th>#</th>
                <th>
                  <HeaderButton label="名称" sortKey="name" active={sortKey} dir={sortDir} onSort={onSort} />
                </th>
                <th>
                  <HeaderButton label="价格 SOL" sortKey="price" active={sortKey} dir={sortDir} onSort={onSort} />
                </th>
                <th>
                  <HeaderButton label="涨跌" sortKey="change" active={sortKey} dir={sortDir} onSort={onSort} />
                </th>
                <th>
                  <HeaderButton label="成交额" sortKey="volume" active={sortKey} dir={sortDir} onSort={onSort} />
                </th>
                <th>
                  <HeaderButton label="市值" sortKey="mcap" active={sortKey} dir={sortDir} onSort={onSort} />
                </th>
                <th>
                  <HeaderButton label="曲线" sortKey="progress" active={sortKey} dir={sortDir} onSort={onSort} />
                </th>
                <th>走势</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row, i) => (
                <MarketRow
                  key={row.symbol}
                  row={row}
                  rank={i + 1}
                  starred={starSet.has(row.symbol)}
                  onToggle={toggleStar}
                  onOpen={(item) => navigate(`/trade/${encodeURIComponent(item.mint || item.symbol)}`)}
                />
              ))}
            </tbody>
          </table>
          {rows.length === 0 ? <p className="mk-empty">{emptyCopy(tab, list)}</p> : null}
        </div>
      ) : null}
    </div>
  );
}

function noteFor(list: MarketList | null, err: string): string {
  if (!list && err) return `市场暂时读不到：${err}`;
  if (!list) return "纸面监控";
  const bits = ["报价 SOL"];
  if (!list.usd_available) bits.push("无美元价");
  if (list.discovery === "off") bits.push("发现关闭，列表是本地纸面监控");
  else bits.push(`发现 ${list.discovery_active || list.discovery}`);
  if (list.provider === "mock") bits.push("mock 行情没有曲线市值");
  if (err) bits.push("刷新失败，显示上次数据");
  return bits.join(" · ");
}

function emptyCopy(tab: Tab, list: MarketList): string {
  if (list.empty) return "暂无监控代币。发现关闭时不会有新币写入自选。";
  if (tab === "watch") return "观察池是空的。点星标后，代币会留在这台浏览器里，不会改策略参数。";
  if (tab === "new") return "还没有新发现的币。发现关闭时，内置纸面币不会出现在这一栏。";
  if (tab === "gainers" || tab === "losers") return "这一栏没有可排序的涨跌。";
  return "没有匹配的代币。";
}
