import type { MonitorRow } from "@/types/contracts";

interface Props {
  rows: MonitorRow[];
  active: string;
  onSelect: (symbol: string) => void;
}

function fmtPct(bps: number | null): string {
  if (bps == null) return "—";
  return `${(bps / 100).toFixed(1)}%`;
}

function fmtSol(n: number): string {
  if (!Number.isFinite(n)) return "—";
  if (n === 0) return "0";
  if (n >= 10) return n.toFixed(2);
  return n.toFixed(3);
}

function fmtBps(n: number | null): string {
  if (n == null || !Number.isFinite(n)) return "—";
  return String(Math.round(n));
}

export function WatchlistTable({ rows, active, onSelect }: Props) {
  return (
    <div className="symbol-list watchlist-monitor">
      <div className="panel-title">新币 / 自选</div>
      <div className="watchlist-head" aria-hidden="true">
        <span>标的</span>
        <span>进度</span>
        <span>1m买</span>
        <span>1m卖</span>
        <span>冲击</span>
      </div>
      <ul>
        {rows.map((r) => (
          <li key={r.symbol}>
            <button
              className={r.symbol === active ? "active" : ""}
              onClick={() => onSelect(r.symbol)}
              type="button"
            >
              <span className="watch-row">
                <span className="sym">{r.symbol}</span>
                <span className="num">{fmtPct(r.progress_bps)}</span>
                <span className="num buy">{fmtSol(r.buy_notional_1m)}</span>
                <span className="num sell">{fmtSol(r.sell_notional_1m)}</span>
                <span className="num">{fmtBps(r.estimated_impact_bps)}</span>
              </span>
              <span className="watch-tags">
                {(r.tags ?? []).slice(0, 4).map((t) => (
                  <span key={t} className={`curve-chip ${t === "migrated" || t === "amm" ? "migrated" : ""}`}>
                    {t}
                  </span>
                ))}
              </span>
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}
