import { useState } from "react";
import { usePaperPerformance } from "@/hooks/usePaperPerformance";
import type { EquityPoint } from "@/types/contracts";

function pct(n: number | null | undefined, digits = 1): string {
  if (n == null || !Number.isFinite(n)) return "—";
  return `${n.toFixed(digits)}%`;
}

function rate(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n)) return "—";
  return `${(n * 100).toFixed(1)}%`;
}

function num(n: number | null | undefined, digits = 2): string {
  if (n == null || !Number.isFinite(n)) return "—";
  return n.toFixed(digits);
}

function EquitySpark({ points }: { points: EquityPoint[] }) {
  if (points.length < 2) return null;
  const w = 160;
  const h = 28;
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
    <svg className="equity-spark" width={w} height={h} viewBox={`0 0 ${w} ${h}`} aria-hidden>
      <path d={d} fill="none" stroke="currentColor" strokeWidth="1.4" />
    </svg>
  );
}

interface Props {
  compact?: boolean;
}

export function PaperStatsPanel({ compact }: Props) {
  const [mcOn, setMcOn] = useState(false);
  const { stats, err } = usePaperPerformance(2500, mcOn);
  const empty = !stats || stats.empty || stats.trade_count === 0;
  const mc = stats?.monte_carlo;
  const sampleOk = Boolean(stats?.sample_ok);
  const journal = stats?.journal ?? stats?.recent ?? [];

  return (
    <section className={`paper-stats ${compact ? "compact" : ""}`} aria-label="paper performance">
      <header>
        <h2>成功概率 · 纸面模拟</h2>
        <span className="muted tiny">非承诺</span>
        <label className="mc-toggle">
          <input type="checkbox" checked={mcOn} onChange={(e) => setMcOn(e.target.checked)} />
          蒙特卡洛
        </label>
      </header>
      {err ? <p className="error tiny">{err}</p> : null}
      {empty ? (
        <p className="muted tiny">暂无平仓样本。纸面开平仓后显示胜率 / 期望 / 回撤。</p>
      ) : (
        <>
          <div className="paper-stats-row">
            <dl>
              <div>
                <dt>胜率</dt>
                <dd>{rate(stats.win_rate)}</dd>
              </div>
              <div>
                <dt>笔数</dt>
                <dd>
                  {stats.trade_count}
                  <span className="muted">
                    {" "}
                    ({stats.wins}胜{stats.losses}负)
                  </span>
                </dd>
              </div>
              <div>
                <dt>期望</dt>
                <dd>
                  {pct(stats.expectancy_pnl_pct)}
                  {stats.expectancy_r != null ? (
                    <span className="muted"> · {num(stats.expectancy_r)}R</span>
                  ) : null}
                </dd>
              </div>
              <div>
                <dt>回撤</dt>
                <dd>{pct(stats.max_drawdown_pct)}</dd>
              </div>
            </dl>
            {stats.equity && stats.equity.length > 1 ? <EquitySpark points={stats.equity} /> : null}
          </div>
          {mcOn ? (
            sampleOk && mc && mc.final_equity_pct_p5 != null ? (
              <p className="mc-line">
                蒙特卡洛 {mc.n_paths} 次{mc.method === "reshuffle" ? "重排" : "重抽样"}：P(权益&gt;起点){" "}
                {rate(mc.p_equity_above_start)}
                {" · "}
                P(触及日亏) {rate(mc.p_hit_day_loss)}
                {" · "}
                区间 p5–p95 {pct(mc.final_equity_pct_p5)} ~ {pct(mc.final_equity_pct_p95)}
              </p>
            ) : (
              <p className="mc-line">样本不足</p>
            )
          ) : null}
          {journal.length ? (
            <table className="journal-table">
              <thead>
                <tr>
                  <th>symbol</th>
                  <th>side</th>
                  <th>pnl%</th>
                  <th>src</th>
                </tr>
              </thead>
              <tbody>
                {journal.slice(compact ? -4 : -6).reverse().map((row, i) => (
                  <tr key={`${row.exit_ts}-${i}`} className={row.pnl >= 0 ? "buy" : "sell"}>
                    <td>{row.symbol}</td>
                    <td>{row.side}</td>
                    <td>{pct(row.pnl_pct * 100)}</td>
                    <td>{row.source ?? ""}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : null}
        </>
      )}
      <p className="muted tiny disclaimer">{stats?.disclaimer ?? "simulation from paper history, not a promise"}</p>
    </section>
  );
}
