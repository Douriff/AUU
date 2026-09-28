import { memo, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { PriceCell } from "@/components/markets/PriceCell";
import { WalletSignalCard } from "@/components/wallet/WalletSignalCard";
import { Sparkline } from "@/components/markets/Sparkline";
import { marketProvider } from "@/providers/HttpWsProvider";
import type { UniverseMover, UniverseRow } from "@/types/contracts";
import { truncateMint } from "@/venue";

const STAR_KEY = "auu:universe-stars";
const PAGE = 60;

type Tab = "hot" | "new" | "graduating" | "graduated" | "gainers" | "losers" | "watch";
type SortKey = "name" | "price" | "change5m" | "change1h" | "change24" | "volume" | "mcap" | "progress" | "age";

const TABS: { id: Tab; label: string }[] = [
  { id: "hot", label: "热门" },
  { id: "new", label: "新币" },
  { id: "graduating", label: "即将毕业" },
  { id: "graduated", label: "已毕业" },
  { id: "gainers", label: "涨幅榜" },
  { id: "losers", label: "跌幅榜" },
  { id: "watch", label: "观察池" },
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
  const body = `${Math.abs(pct).toFixed(1)}%`;
  if (pct > 0) return `+${body}`;
  if (pct < 0) return `-${body}`;
  return body;
}

function formatUsd(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n)) return "—";
  const a = Math.abs(n);
  if (a >= 1e9) return `$${(n / 1e9).toFixed(2)}B`;
  if (a >= 1e6) return `$${(n / 1e6).toFixed(2)}M`;
  if (a >= 1e3) return `$${(n / 1e3).toFixed(1)}K`;
  if (a >= 1) return `$${n.toFixed(2)}`;
  if (a >= 0.01) return `$${n.toFixed(3)}`;
  if (a === 0) return "$0";
  return `$${n.toPrecision(2)}`;
}

function formatAge(sec: number | null | undefined): string {
  if (sec == null || !Number.isFinite(sec)) return "—";
  if (sec < 60) return `${sec}秒`;
  if (sec < 3600) return `${Math.floor(sec / 60)}分`;
  if (sec < 86400) return `${Math.floor(sec / 3600)}时`;
  return `${Math.floor(sec / 86400)}天`;
}

function formatHolders(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n) || n <= 0) return "—";
  if (n >= 1000) return `${(n / 1000).toFixed(1)}K`;
  return String(n);
}

function tone(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n) || n === 0) return "flat";
  return n > 0 ? "up" : "down";
}

function mergeRows(prev: UniverseRow[], fresh: UniverseRow[]): UniverseRow[] {
  const byMint = new Map(fresh.map((row) => [row.mint, row]));
  const seen = new Set<string>();
  const next = prev.map((row) => {
    seen.add(row.mint);
    return byMint.get(row.mint) ?? row;
  });
  for (const row of fresh) {
    if (!seen.has(row.mint)) next.push(row);
  }
  return next;
}

const MarketRow = memo(function MarketRow({
  row,
  rank,
  starred,
  onToggle,
  onOpen,
}: {
  row: UniverseRow;
  rank: number;
  starred: boolean;
  onToggle: (mint: string) => void;
  onOpen: (row: UniverseRow) => void;
}) {
  const progress = row.graduated ? 100 : row.progress_pct;
  return (
    <tr className="mk-row" onClick={() => onOpen(row)}>
      <td className="mk-star">
        <button
          type="button"
          className={starred ? "is-on" : ""}
          aria-pressed={starred}
          aria-label={starred ? `取消观察 ${row.symbol}` : `加入观察池 ${row.symbol}`}
          onClick={(event) => {
            event.stopPropagation();
            onToggle(row.mint);
          }}
        >
          {starred ? "★" : "☆"}
        </button>
      </td>
      <td className="mk-rank">{rank}</td>
      <td className="mk-name">
        <span className="mk-token">
          {row.image ? <img src={row.image} alt="" /> : <i aria-hidden="true">{(row.symbol || "?").slice(0, 1)}</i>}
          <span>
            <strong>{row.symbol}</strong>
            <em>
              {row.name} · {truncateMint(row.mint, 4, 4)}
            </em>
          </span>
        </span>
      </td>
      <td className="mk-num mk-age">{formatAge(row.age_sec)}</td>
      <td className="mk-num mk-px">
        <PriceCell price={row.price_sol} />
        <small>{formatUsd(row.price_usd)}</small>
      </td>
      <td className={`mk-num ${tone(row.change_5m)}`}>{formatPct(row.change_5m)}</td>
      <td className={`mk-num ${tone(row.change_1h)}`}>{formatPct(row.change_1h)}</td>
      <td className={`mk-num ${tone(row.change_24h)}`}>{formatPct(row.change_24h)}</td>
      <td className="mk-num">{formatUsd(row.volume_24h_usd)}</td>
      <td className="mk-num">{row.market_cap_usd != null ? formatUsd(row.market_cap_usd) : formatUsd(row.market_cap_sol)}</td>
      <td className="mk-num">{formatHolders(row.holders)}</td>
      <td className="mk-num mk-progress">
        {progress == null ? (
          "—"
        ) : (
          <span className="mk-prog">
            <span className="mk-prog-track">
              <i style={{ width: `${Math.max(0, Math.min(100, progress))}%` }} />
            </span>
            <b>{row.graduated ? row.venue || "已毕业" : `${progress.toFixed(0)}%`}</b>
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
  active: SortKey | "";
  dir: "asc" | "desc";
  onSort: (key: SortKey) => void;
}) {
  const on = active === sortKey;
  return (
    <button type="button" onClick={() => onSort(sortKey)} aria-sort={on ? (dir === "desc" ? "descending" : "ascending") : "none"}>
      {label}
      <span aria-hidden="true">{on ? (dir === "desc" ? " ↓" : " ↑") : ""}</span>
    </button>
  );
}

export function MarketsPage() {
  const navigate = useNavigate();
  const [tab, setTab] = useState<Tab>("hot");
  const [query, setQuery] = useState("");
  const [debounced, setDebounced] = useState("");
  const [sortKey, setSortKey] = useState<SortKey | "">("");
  const [sortDir, setSortDir] = useState<"asc" | "desc">("desc");
  const [stars, setStars] = useState<string[]>(() => readStars());
  const [rows, setRows] = useState<UniverseRow[]>([]);
  const [movers, setMovers] = useState<UniverseMover[]>([]);
  const [total, setTotal] = useState(0);
  const [err, setErr] = useState("");
  const [loading, setLoading] = useState(true);
  const sentinel = useRef<HTMLDivElement>(null);
  const rowsRef = useRef(rows);
  rowsRef.current = rows;

  useEffect(() => {
    localStorage.setItem(STAR_KEY, JSON.stringify(stars));
  }, [stars]);

  useEffect(() => {
    const id = window.setTimeout(() => setDebounced(query.trim()), 250);
    return () => window.clearTimeout(id);
  }, [query]);

  const starKey = stars.join(",");
  const starSet = useMemo(() => new Set(stars), [stars]);

  useEffect(() => {
    let stop = false;
    const load = async (offset: number, replace: boolean) => {
      try {
        const page = await marketProvider.getUniverse({
          tab,
          offset,
          limit: PAGE,
          q: debounced,
          sort: sortKey,
          dir: sortKey ? sortDir : "",
          mints: tab === "watch" ? starKey : "",
        });
        if (stop) return;
        setMovers(page.movers || []);
        setTotal(page.total);
        setErr(page.error || "");
        setRows((prev) => (replace ? page.items : mergeRows(prev, page.items)));
        setLoading(false);
      } catch (e) {
        if (!stop) {
          setErr(e instanceof Error ? e.message : "行情暂时不可用");
          setLoading(false);
        }
      }
    };
    setLoading(true);
    void load(0, true);
    const id = window.setInterval(() => void load(0, rowsRef.current.length <= PAGE), 8000);
    return () => {
      stop = true;
      window.clearInterval(id);
    };
  }, [tab, debounced, sortKey, sortDir, starKey]);

  useEffect(() => {
    const node = sentinel.current;
    if (!node) return;
    const observer = new IntersectionObserver((entries) => {
      if (!entries.some((entry) => entry.isIntersecting)) return;
      if (rows.length >= total || loading) return;
      void marketProvider
        .getUniverse({
          tab,
          offset: rows.length,
          limit: PAGE,
          q: debounced,
          sort: sortKey,
          dir: sortKey ? sortDir : "",
          mints: tab === "watch" ? starKey : "",
        })
        .then((page) => {
          setTotal(page.total);
          setRows((prev) => mergeRows(prev, page.items));
        })
        .catch(() => undefined);
    });
    observer.observe(node);
    return () => observer.disconnect();
  }, [rows.length, total, loading, tab, debounced, sortKey, sortDir, starKey]);

  const onSort = (key: SortKey) => {
    if (key === sortKey) setSortDir((d) => (d === "desc" ? "asc" : "desc"));
    else {
      setSortKey(key);
      setSortDir(key === "name" || key === "age" ? "asc" : "desc");
    }
  };

  const toggleStar = (mint: string) => {
    setStars((prev) => (prev.includes(mint) ? prev.filter((item) => item !== mint) : [...prev, mint]));
  };

  return (
    <div className="markets-page mk-dense">
      <div className="mk-ticker" aria-label="涨幅条">
        <span>涨幅</span>
        <div>
          {movers.length === 0 ? (
            <em>等待涨跌数据</em>
          ) : (
            movers.map((row) => (
              <button key={row.mint} type="button" onClick={() => navigate(`/trade/${encodeURIComponent(row.mint)}`)}>
                <b>{row.symbol}</b>
                <i className={tone(row.change_24h)}>{formatPct(row.change_24h)}</i>
              </button>
            ))
          )}
        </div>
      </div>

      <header className="mk-head">
        <div>
          <h1>市场</h1>
          <p className="mk-note">
            {err ? `行情：${err}` : "pump.fun 全站 · 报价 SOL / USD · 与发现模式无关"}
            {loading && rows.length === 0 ? " · 读取中" : ""}
          </p>
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

      <WalletSignalCard mint={rows[0]?.mint} priceSol={rows[0]?.price_sol} symbol={rows[0]?.symbol} compact />

      <div className="mk-tabs" role="tablist" aria-label="市场筛选">
        {TABS.map((item) => (
          <button
            key={item.id}
            type="button"
            role="tab"
            aria-selected={tab === item.id}
            className={tab === item.id ? "is-on" : ""}
            onClick={() => {
              setTab(item.id);
              setSortKey("");
            }}
          >
            {item.label}
          </button>
        ))}
      </div>

      <div className="mk-table-wrap">
        <table className="mk-table">
          <thead>
            <tr>
              <th aria-label="观察" />
              <th>#</th>
              <th>
                <HeaderButton label="代币" sortKey="name" active={sortKey} dir={sortDir} onSort={onSort} />
              </th>
              <th>
                <HeaderButton label="年龄" sortKey="age" active={sortKey} dir={sortDir} onSort={onSort} />
              </th>
              <th>
                <HeaderButton label="价格" sortKey="price" active={sortKey} dir={sortDir} onSort={onSort} />
              </th>
              <th>
                <HeaderButton label="5分" sortKey="change5m" active={sortKey} dir={sortDir} onSort={onSort} />
              </th>
              <th>
                <HeaderButton label="1时" sortKey="change1h" active={sortKey} dir={sortDir} onSort={onSort} />
              </th>
              <th>
                <HeaderButton label="24时" sortKey="change24" active={sortKey} dir={sortDir} onSort={onSort} />
              </th>
              <th>
                <HeaderButton label="成交额" sortKey="volume" active={sortKey} dir={sortDir} onSort={onSort} />
              </th>
              <th>
                <HeaderButton label="市值" sortKey="mcap" active={sortKey} dir={sortDir} onSort={onSort} />
              </th>
              <th>持有人</th>
              <th>
                <HeaderButton label="曲线" sortKey="progress" active={sortKey} dir={sortDir} onSort={onSort} />
              </th>
              <th>走势</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row, index) => (
              <MarketRow
                key={row.mint}
                row={row}
                rank={index + 1}
                starred={starSet.has(row.mint)}
                onToggle={toggleStar}
                onOpen={(item) => navigate(`/trade/${encodeURIComponent(item.mint)}`)}
              />
            ))}
          </tbody>
        </table>
        {rows.length === 0 && !loading ? (
          <p className="mk-empty">
            {tab === "watch" ? "观察池是空的。点星标后留在这台浏览器里。" : "这一栏暂时没有代币。"}
          </p>
        ) : null}
        <div ref={sentinel} className="mk-more" />
      </div>
    </div>
  );
}
