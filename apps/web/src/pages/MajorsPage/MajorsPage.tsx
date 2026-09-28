import { useEffect, useState } from "react";
import { marketProvider } from "@/providers/HttpWsProvider";
import type { MajorsBoard, MajorsQuote } from "@/types/contracts";

function formatPx(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n)) return "—";
  const a = Math.abs(n);
  const digits = a >= 1000 ? 1 : a >= 1 ? 2 : 4;
  return n.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });
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

function Cell({ quote }: { quote: MajorsQuote | undefined }) {
  if (!quote || quote.status === "unavailable") return <span className="mj-off">不可用</span>;
  if (quote.status !== "ok" || quote.last == null) return <span className="mj-off">—</span>;
  return (
    <span className="mj-cell">
      <b>{formatPx(quote.last)}</b>
      <i className={tone(quote.change_24h)}>{formatPct(quote.change_24h)}</i>
      <em>{formatVol(quote.volume_24h)}</em>
    </span>
  );
}

export function MajorsPage() {
  const [board, setBoard] = useState<MajorsBoard | null>(null);
  const [err, setErr] = useState("");

  useEffect(() => {
    let stop = false;
    const tick = async () => {
      try {
        const data = await marketProvider.getMajors();
        if (!stop) {
          setBoard(data);
          setErr("");
        }
      } catch (e) {
        if (!stop) setErr(e instanceof Error ? e.message : "行情读取失败");
      }
    };
    void tick();
    const id = window.setInterval(() => void tick(), 4000);
    return () => {
      stop = true;
      window.clearInterval(id);
    };
  }, []);

  const venues = board?.venues ?? [];

  return (
    <div className="majors-page">
      <header className="mj-top">
        <div>
          <h1>大盘</h1>
          <p>公开行情，只读。单所超时或被阻断时记为不可用，其余继续刷新。</p>
        </div>
        <div className="mj-health" aria-label="交易所状态">
          {venues.map((venue) => (
            <span key={venue.id} className={venue.status === "ok" ? "is-ok" : "is-down"}>
              {venue.label} {venue.status_label}
            </span>
          ))}
          <span className="mj-badge">LIVE OFF</span>
        </div>
      </header>
      {err && <p className="mj-err">{err}</p>}
      <div className="mj-table-wrap">
        <table className="mj-table">
          <thead>
            <tr>
              <th>币种</th>
              {venues.map((venue) => (
                <th key={venue.id}>
                  {venue.label}
                  <small>价格 / 24h / 成交额</small>
                </th>
              ))}
              <th>跨所价差</th>
            </tr>
          </thead>
          <tbody>
            {(board?.rows ?? []).map((row) => (
              <tr key={row.base}>
                <td className="mj-base">{row.base}</td>
                {venues.map((venue) => (
                  <td key={venue.id}>
                    <Cell quote={row.quotes[venue.id]} />
                  </td>
                ))}
                <td className="num">{row.spread_bps == null ? "—" : `${row.spread_bps.toFixed(1)} bps`}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {!board && !err && <p className="mj-err">正在读取公开行情…</p>}
      </div>
    </div>
  );
}
