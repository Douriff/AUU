import { useEffect, useState } from "react";
import { usePaperPerformance } from "@/hooks/usePaperPerformance";
import { useStrategyConfig } from "@/hooks/useStrategyConfig";
import type { EquityPoint } from "@/types/contracts";

const STATS_KEY = "auu:show_paper_stats";
const MC_KEY = "auu:show_monte_carlo";

export function readShowPaperStats(): boolean {
  try {
    const v = localStorage.getItem(STATS_KEY);
    return v == null ? true : v === "1";
  } catch {
    return true;
  }
}

export function readShowMonteCarlo(): boolean {
  try {
    return localStorage.getItem(MC_KEY) === "1";
  } catch {
    return false;
  }
}

export function writeShowPaperStats(v: boolean) {
  localStorage.setItem(STATS_KEY, v ? "1" : "0");
}

export function writeShowMonteCarlo(v: boolean) {
  localStorage.setItem(MC_KEY, v ? "1" : "0");
}

function pct(n: number | null | undefined, digits = 1): string {
  if (n == null || !Number.isFinite(n)) return "—";
  return `${n.toFixed(digits)}%`;
}

function rate(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n)) return "—";
  return `${(n * 100).toFixed(1)}%`;
}

function num(n: number | null | undefined, digits = 4): string {
  if (n == null || !Number.isFinite(n)) return "—";
  return n.toPrecision(digits);
}

function EquitySpark({ points }: { points: EquityPoint[] }) {
  if (points.length < 2) return null;
  const w = 180;
  const h = 32;
  const ys = points.map((p) => p.equity);
  const min = Math.min(...ys);
  const max = Math.max(...ys);
  const span = max - min || 1e-9;
  const d = points
    .map((p, i) => {
      const x = (i / (points.length - 1)) * w;
      const y = h - ((p.equity - min) / span) * (h - 2) - 1;
      return `${i === 0 ? "M" : "L"}${x.toFixed(1)} ${y.toFixed(1)}`;
    })
    .join(" ");
  return (
    <svg className="equity-spark" width={w} height={h} viewBox={`0 0 ${w} ${h}`} aria-label="equity">
      <path d={d} fill="none" stroke="currentColor" strokeWidth="1.4" />
    </svg>
  );
}

interface Props {
  compact?: boolean;
}

export function PaperStatsPanel({ compact }: Props) {
  const [showStats, setShowStats] = useState(true);
  const [mcOn, setMcOn] = useState(false);
  const { stats, err } = usePaperPerformance(2500, mcOn);
  const { autoPaperOrders, tradingState } = useStrategyConfig();

  useEffect(() => {
    setShowStats(readShowPaperStats());
    setMcOn(readShowMonteCarlo());
  }, []);

  if (!showStats) return null;

  const empty = !stats || stats.empty || (stats.n_trades ?? stats.trade_count ?? 0) === 0;
  const n = stats?.n_trades ?? stats?.trade_count ?? 0;
  const mc = stats?.monte_carlo;
  const sampleOk = Boolean(stats?.sample_ok);
  const journal = stats?.journal ?? [];

  return (
    <section className={`paper-stats ${compact ? "compact" : ""}`} aria-label="paper stats">
      <header>
        <h2>PaperStats</h2>
        <span className="muted tiny">纸面 · 非承诺</span>
        <span className="trading-state" data-state={autoPaperOrders ? "active" : "halted"} title="autopaper">
          autopaper {autoPaperOrders ? "on" : "off"}
        </span>
        <span className="muted tiny">{tradingState}</span>
        <label className="mc-toggle" title="show_monte_carlo · default off">
          <input
            type="checkbox"
            checked={mcOn}
            onChange={(e) => {
              const v = e.target.checked;
              setMcOn(v);
              writeShowMonteCarlo(v);
            }}
          />
          蒙特卡洛
        </label>
      </header>
      {err ? <p className="error tiny">{err}</p> : null}
      {empty ? (
        <p className="muted tiny">暂无已平仓纸面交易</p>
      ) : (
        <>
          <div className="paper-stats-row">
            <dl>
              <div>
                <dt>胜率</dt>
                <dd>{rate(stats.win_rate)}</dd>
              </div>
              <div>
                <dt>期望</dt>
                <dd>{num(stats.expectancy)}</dd>
              </div>
              <div>
                <dt>回撤</dt>
                <dd>{pct(stats.max_drawdown_pct)}</dd>
              </div>
              <div>
                <dt>笔数</dt>
                <dd>
                  {n}
                  <span className="muted">
                    {" "}
                    ({stats.wins}胜{stats.losses}负)
                  </span>
                </dd>
              </div>
            </dl>
            {stats.equity && stats.equity.length > 1 ? <EquitySpark points={stats.equity} /> : null}
          </div>
          {journal.length ? (
            <table className="journal-table">
              <thead>
                <tr>
                  <th>symbol</th>
                  <th>entry</th>
                  <th>exit</th>
                  <th>pnl</th>
                  <th>tags</th>
                </tr>
              </thead>
              <tbody>
                {journal.slice(compact ? -4 : -8).reverse().map((row) => (
                  <tr key={row.id} className={row.pnl >= 0 ? "buy" : "sell"}>
                    <td>{row.symbol}</td>
                    <td>{num(row.entry_price, 3)}</td>
                    <td>{num(row.exit_price, 3)}</td>
                    <td>{num(row.pnl)}</td>
                    <td>{(row.tags ?? []).join(",")}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : null}
        </>
      )}
      {mcOn ? (
        sampleOk && mc && mc.p50_pnl != null ? (
          <p className="mc-line">
            MC {mc.n_paths} {mc.method ?? "shuffle"}：p50 pnl {num(mc.p50_pnl)} · p05–p95 {num(mc.p05_pnl)} ~{" "}
            {num(mc.p95_pnl)} · p50 DD {pct(mc.p50_dd)}
          </p>
        ) : (
          <p className="mc-line">样本不足</p>
        )
      ) : null}
    </section>
  );
}
