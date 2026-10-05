import { Empty, SkRows } from "@/components/ui/Skeleton";
import { useEffect, useState, type KeyboardEvent } from "react";
import { useNavigate } from "react-router-dom";
import { useTranslation } from "react-i18next";
import i18n from "i18next";
import { fmtFixed, fmtKMB, fmtMax } from "@/i18n/format";
import { errText } from "@/i18n/errors";
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
  { id: "cross", label: "majors.cross" },
];

function formatPx(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n)) return "—";
  const a = Math.abs(n);
  const digits = a >= 1000 ? 2 : a >= 1 ? 3 : a >= 0.01 ? 4 : 6;
  return fmtMax(n, digits);
}

function formatVol(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n)) return "—";
  return fmtKMB(n);
}

function formatPct(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n)) return "—";
  const pct = n * 100;
  const body = `${fmtFixed(Math.abs(pct), 2)}%`;
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
  const navigate = useNavigate();
  const { t } = useTranslation();
  // a row opens the coin's trade view (K 线 + 订单簿 + 纸面交易), carrying the venue tab
  const openCoin = (base: string) => {
    const q = new URLSearchParams({ symbol: base });
    if (venue !== "cross") q.set("venue", venue);
    navigate(`/?${q}`);
  };

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
        if (!stop) setErr(errText(e, "majors.loadFailed"));
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
          <h1>{t("majors.title")}</h1>
          <p>{t("majors.sub")}</p>
        </div>
        <div className="mj-health" aria-label={t("majors.health")}>
          {health.map((row) => (
            <span key={row.id} className={row.status === "ok" ? "is-ok" : "is-down"}>
              <i aria-hidden="true" />
              {row.label} {row.status === "ok" ? t("majors.up") : t("majors.down")}
            </span>
          ))}
        </div>
      </header>

      <div className="mj-tools">
        <div className="mk-tabs" role="tablist" aria-label={t("majors.venue")}>
          {VENUES.map((item) => (
            <button key={item.id} type="button" className={venue === item.id ? "is-on" : ""} onClick={() => setVenue(item.id)}>
              {item.id === "cross" ? t(item.label) : item.label}
            </button>
          ))}
        </div>
        {venue !== "cross" ? (
          <div className="mk-tabs" aria-label={t("majors.bucket")}>
            {(
              [
                ["all", t("majors.all")],
                ["gainers", t("majors.gainers")],
                ["losers", t("majors.losers")],
              ] as const
            ).map(([id, label]) => (
              <button key={id} type="button" className={bucket === id ? "is-on" : ""} onClick={() => setBucket(id)}>
                {label}
              </button>
            ))}
          </div>
        ) : null}
        <label className="mk-search">
          <span className="sr-only">{t("majors.search")}</span>
          <input value={query} onChange={(e) => setQuery(e.target.value)} placeholder={t("majors.searchPh")} type="search" />
        </label>
      </div>

      {err ? <p className="mj-err">{err}</p> : null}
      {down ? <p className="mj-err">{t("majors.venueDown")}</p> : null}

      <div className="mj-table-wrap">
        <table className="mj-table">
          <thead>
            {venue === "cross" ? (
              <tr>
                <th>#</th>
                <th>
                  <button type="button" onClick={() => onSort("symbol")}>{t("majors.col.coin")}</button>
                </th>
                <th>{t("majors.col.bestBidVenue")}</th>
                <th>{t("majors.col.bidPx")}</th>
                <th>{t("majors.col.bestAskVenue")}</th>
                <th>{t("majors.col.askPx")}</th>
                <th>
                  <button type="button" onClick={() => onSort("spread")}>{t("majors.col.spread")}</button>
                </th>
                <th>{t("majors.col.venues")}</th>
              </tr>
            ) : (
              <tr>
                <th>#</th>
                <th>
                  <button type="button" onClick={() => onSort("symbol")}>{t("majors.col.pair")}</button>
                </th>
                <th>
                  <button type="button" onClick={() => onSort("price")}>{t("majors.col.last")}</button>
                </th>
                <th>
                  <button type="button" onClick={() => onSort("change")}>24h</button>
                </th>
                <th>
                  <button type="button" onClick={() => onSort("volume")}>{t("majors.col.volume")}</button>
                </th>
                <th>{t("majors.col.bid")}</th>
                <th>{t("majors.col.ask")}</th>
              </tr>
            )}
          </thead>
          <tbody>
            {items.map((row, index) =>
              venue === "cross" ? (
                <CrossRow key={row.base} row={row} rank={index + 1} onOpen={openCoin} />
              ) : (
                <TickerRow key={row.symbol} row={row} rank={index + 1} onOpen={openCoin} />
              ),
            )}
          </tbody>
        </table>
        {!board && !err ? <SkRows rows={12} cols={7} h={36} /> : null}
        {board && items.length === 0 && !down ? <Empty icon="search" title={t("majors.noMatch")} hint={t("majors.noMatchHint")} /> : null}
      </div>
    </div>
  );
}

function rowProps(base: string, onOpen: (base: string) => void) {
  return {
    className: "mj-row-link",
    tabIndex: 0,
    role: "link",
    title: i18n.t("majors.rowTitle", { base }),
    onClick: () => onOpen(base),
    onKeyDown: (e: KeyboardEvent<HTMLTableRowElement>) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        onOpen(base);
      }
    },
  };
}

function TickerRow({ row, rank, onOpen }: { row: MajorsTicker; rank: number; onOpen: (base: string) => void }) {
  return (
    <tr {...rowProps(row.base, onOpen)}>
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

function CrossRow({ row, rank, onOpen }: { row: MajorsTicker; rank: number; onOpen: (base: string) => void }) {
  return (
    <tr {...rowProps(row.base, onOpen)}>
      <td className="mj-rank">{rank}</td>
      <td className="mj-base">{row.base}</td>
      <td>{row.bid_venue || "—"}</td>
      <td className="num">{formatPx(row.bid)}</td>
      <td>{row.ask_venue || "—"}</td>
      <td className="num">{formatPx(row.ask)}</td>
      <td className="num">{row.spread_bps == null ? "—" : `${fmtFixed(row.spread_bps, 1)} bps`}</td>
      <td>{(row.venues || []).join(" / ") || "—"}</td>
    </tr>
  );
}
