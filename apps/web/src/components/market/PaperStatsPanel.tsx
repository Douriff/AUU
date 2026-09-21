import { usePaperPerformance } from "@/hooks/usePaperPerformance";

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

interface Props {
  compact?: boolean;
}

export function PaperStatsPanel({ compact }: Props) {
  const { stats, err } = usePaperPerformance();
  const empty = !stats || stats.empty || stats.trade_count === 0;
  const mc = stats?.monte_carlo;

  return (
    <section className={`paper-stats ${compact ? "compact" : ""}`} aria-label="paper performance">
      <header>
        <h2>成功概率 · 纸面模拟</h2>
        <span className="muted tiny">非承诺</span>
      </header>
      {err ? <p className="error tiny">{err}</p> : null}
      {empty ? (
        <p className="muted tiny">暂无平仓样本。纸面开平仓后显示胜率 / 期望 / 回撤。</p>
      ) : (
        <>
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
            {stats.sharpe_like != null ? (
              <div>
                <dt>夏普样</dt>
                <dd>{num(stats.sharpe_like)}</dd>
              </div>
            ) : null}
          </dl>
          {mc ? (
            <p className="mc-line">
              蒙特卡洛 {mc.n_paths} 次重抽样：P(权益&gt;起点) {rate(mc.p_equity_above_start)}
              {" · "}
              P(触及日亏) {rate(mc.p_hit_day_loss)}
              {" · "}
              区间 p5–p95 {pct(mc.final_equity_pct_p5)} ~ {pct(mc.final_equity_pct_p95)}
            </p>
          ) : null}
        </>
      )}
      <p className="muted tiny disclaimer">{stats?.disclaimer ?? "simulation from paper history, not a promise"}</p>
    </section>
  );
}
