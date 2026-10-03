import { Empty, SkRows } from "@/components/ui/Skeleton";
import { useEffect, useState } from "react";
import { marketProvider } from "@/providers/HttpWsProvider";
import type { MajorsTicker, MajorsTickerBoard } from "@/types/contracts";

type Venue = "binance" | "okx" | "bybit" | "coinbase" | "cross";
type Bucket = "all" | "gainers" | "losers";
type SortKey = "volume" | "change" | "price" | "symbol" | "spread";

const VENUES: { id: Venue; label: string }[] = [
  { id: "binance", label: "Binance" },
  { id: "okx", label: "OKX" },
  { id: "bybit", label: "Bybit" },
  { id: "coinbase", label: "Coinbase" },
  { id: "cross", label: "跨所对照" },
];

function formatPx(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n)) return "—";
  const a = Math.abs(n);
  const digits = a >= 1000 ? 2 : a >= 1 ? 3 : a >= 0.01 ? 4 : 6;
  return n.toLocaleString("en-US", { maximumFractionDigits: digits });
}

function formatVol(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n)) return "—";
  const a = Math.abs(n);
  if (a >= 1e9) return `${(n / 1e9).toFixed(2)}B`;
  if (a >= 1e6) return `${(n / 1e6).toFixed(2)}M`;
  if (a >= 1e3) return `${(n / 1e3).toFixed(1)}K`;
  return n.toFixed(0);
}

function formatPct(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n)) return "—";
  const pct = n * 100;
  const body = `${Math.abs(pct).toFixed(2)}%`;
  if (pct > 0) return `+${body}`;
  if (pct < 0) return `-${body}`;
  return body;
}

function tone(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n) || n === 0) return "flat";
  return n > 0 ? "up" : "down";
}

export function MajorsPage() {
  const [venue, setVenue] = useState<Venue>("okx");
  const [bucket, setBucket] = useState<Bucket>("all");
  const [sort, setSort] = useState<SortKey>("volume");
  const [dir, setDir] = useState<"asc" | "desc">("desc");
  const [query, setQuery] = useState("");
  const [debounced, setDebounced] = useState("");
  const [board, setBoard] = useState<MajorsTickerBoard | null>(null);
  const [err, setErr] = useState("");

  useEffect(() => {
    const id = window.setTimeout(() => setDebounced(query.trim()), 200);
    return () => window.clearTimeout(id);
  }, [query]);

  useEffect(() => {
    let stop = false;
    const tick = async () => {
      try {
        const data = await marketProvider.getMajorsTickers({
          venue,
          limit: 100,
          q: debounced,
          sort: venue === "cross" && sort === "change" ? "spread" : sort,
          dir,
          bucket: venue === "cross" ? "all" : bucket,
        });
        if (!stop) {
          setBoard(data);
          setErr("");
        }
      } catch (e) {
        if (!stop) setErr(e instanceof Error ? e.message : "行情读取失败");
      }
    };
    void tick();
    const id = window.setInterval(() => void tick(), 5000);
    return () => {
      stop = true;
      window.clearInterval(id);
    };
  }, [venue, bucket, sort, dir, debounced]);

  const onSort = (key: SortKey) => {
    if (key === sort) setDir((value) => (value === "desc" ? "asc" : "desc"));
    else {
      setSort(key);
      setDir(key === "symbol" ? "asc" : "desc");
    }
  };

  const health = board?.venues ?? [];
  const down = board?.status !== "ok";
  const items = board?.items ?? [];

  return (
    <div className="majors-page mj-dense pro-page">
      <header className="mj-top pro-head">
        <div>
          <h1>大盘</h1>
          <p>各交易所成交额前 100 的现货交易对 · 公开行情</p>
        </div>
        <div className="mj-health" aria-label="交易所状态">
          {health.map((row) => (
            <span key={row.id} className={row.status === "ok" ? "is-ok" : "is-down"}>
              <i aria-hidden="true" />
              {row.label} {row.status_label}
            </span>
          ))}
        </div>
      </header>

      <div className="mj-tools">
        <div className="mk-tabs" role="tablist" aria-label="交易所">
          {VENUES.map((item) => (
            <button key={item.id} type="button" className={venue === item.id ? "is-on" : ""} onClick={() => setVenue(item.id)}>
              {item.label}
            </button>
          ))}
        </div>
        {venue !== "cross" ? (
          <div className="mk-tabs" aria-label="涨跌">
            {(
              [
                ["all", "全部"],
                ["gainers", "涨幅"],
                ["losers", "跌幅"],
              ] as const
            ).map(([id, label]) => (
              <button key={id} type="button" className={bucket === id ? "is-on" : ""} onClick={() => setBucket(id)}>
                {label}
              </button>
            ))}
          </div>
        ) : null}
        <label className="mk-search">
          <span className="sr-only">搜索</span>
          <input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="搜索币种" type="search" />
        </label>
      </div>

      {err ? <p className="mj-err">{err}</p> : null}
      {down ? <p className="mj-err">这个交易所当前不可用。</p> : null}

      <div className="mj-table-wrap">
        <table className="mj-table">
          <thead>
            {venue === "cross" ? (
              <tr>
                <th>#</th>
                <th>
                  <button type="button" onClick={() => onSort("symbol")}>币种</button>
                </th>
                <th>最优买</th>
                <th>买价</th>
                <th>最优卖</th>
                <th>卖价</th>
                <th>
                  <button type="button" onClick={() => onSort("spread")}>价差</button>
                </th>
                <th>参与交易所</th>
              </tr>
            ) : (
              <tr>
                <th>#</th>
                <th>
                  <button type="button" onClick={() => onSort("symbol")}>交易对</button>
                </th>
                <th>
                  <button type="button" onClick={() => onSort("price")}>最新价</button>
                </th>
                <th>
                  <button type="button" onClick={() => onSort("change")}>24h</button>
                </th>
                <th>
                  <button type="button" onClick={() => onSort("volume")}>成交额</button>
                </th>
                <th>买一</th>
                <th>卖一</th>
              </tr>
            )}
          </thead>
          <tbody>
            {items.map((row, index) =>
              venue === "cross" ? (
                <CrossRow key={row.base} row={row} rank={index + 1} />
              ) : (
                <TickerRow key={row.symbol} row={row} rank={index + 1} />
              ),
            )}
          </tbody>
        </table>
        {!board && !err ? <SkRows rows={12} cols={7} h={36} /> : null}
        {board && items.length === 0 && !down ? <Empty icon="search" title="没有匹配的交易对" hint="换个关键词或交易所试试" /> : null}
      </div>
    </div>
  );
}

function TickerRow({ row, rank }: { row: MajorsTicker; rank: number }) {
  return (
    <tr>
      <td className="mj-rank">{rank}</td>
      <td className="mj-base">{row.base}</td>
      <td className="num">{formatPx(row.last)}</td>
      <td className={`num ${tone(row.change_24h)}`}>{formatPct(row.change_24h)}</td>
      <td className="num">{formatVol(row.volume_24h)}</td>
      <td className="num">{formatPx(row.bid)}</td>
      <td className="num">{formatPx(row.ask)}</td>
    </tr>
  );
}

function CrossRow({ row, rank }: { row: MajorsTicker; rank: number }) {
  return (
    <tr>
      <td className="mj-rank">{rank}</td>
      <td className="mj-base">{row.base}</td>
      <td>{row.bid_venue || "—"}</td>
      <td className="num">{formatPx(row.bid)}</td>
      <td>{row.ask_venue || "—"}</td>
      <td className="num">{formatPx(row.ask)}</td>
      <td className="num">{row.spread_bps == null ? "—" : `${row.spread_bps.toFixed(1)} bps`}</td>
      <td>{(row.venues || []).join(" / ") || "—"}</td>
    </tr>
  );
}
