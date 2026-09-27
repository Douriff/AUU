import { useShadowCompare } from "@/hooks/useShadowCompare";
import type { ShadowCompareColumn, ShadowExitCompare } from "@/types/contracts";

const EXIT_LABEL: Record<string, string> = {
  take_profit: "TP",
  stop_loss: "SL",
  max_hold: "MH",
  sell_pressure: "SP",
  graduation: "毕业",
  orphan: "孤儿",
  other: "其他",
};

function num(n: number | null | undefined, digits = 4): string {
  if (n == null || !Number.isFinite(n)) return "—";
  return n.toPrecision(digits);
}

function pct(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n)) return "—";
  return `${(n * 100).toFixed(1)}%`;
}

function bps(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n)) return "—";
  return n.toFixed(1);
}

function exitMix(col: ShadowCompareColumn): string {
  const rows = (col.by_exit_reason ?? []).filter((r) => r.count > 0);
  if (!rows.length) return "—";
  return rows
    .map((r) => `${EXIT_LABEL[r.reason] ?? r.reason} ${pct(r.pct)}`)
    .join(" · ");
}

function vsMain(rows: ShadowExitCompare[] | undefined): string {
  const active = (rows ?? []).filter((r) => r.shadow_count > 0 || r.main_count > 0);
  if (!active.length) return "—";
  return active
    .map((r) => {
      const name = EXIT_LABEL[r.reason] ?? r.reason;
      return `${name} ${pct(r.shadow_pct)}/${pct(r.main_pct)}`;
    })
    .join(" · ");
}

interface Props {
  compact?: boolean;
  pollMs?: number;
}

export function ShadowComparePanel({ compact, pollMs = 0 }: Props) {
  const { report, err, loading, refresh } = useShadowCompare(pollMs);
  const columns = report ? [report.main, ...report.sets] : [];

  return (
    <div className={`shadow-compare-panel ${compact ? "compact" : ""}`} aria-label="shadow parameter comparison">
      <header>
        <h3>影子参数对照</h3>
        <span className="muted tiny">纸面 · 不开启实盘</span>
        <button type="button" className="ghost tiny" onClick={() => void refresh()} disabled={loading}>
          {loading ? "刷新中…" : "刷新"}
        </button>
      </header>
      {err ? <p className="error tiny">{err}</p> : null}
      {!report ? (
        <p className="muted tiny">加载对照…</p>
      ) : (
        <>
          <p className="muted tiny shadow-note">{report.note}</p>
          <p className="muted tiny">
            {report.enabled ? `已启用 ${report.sets.length}/${report.max_sets} 组` : "对照关闭（默认）"}
            {" · "}
            progress {report.progress_locked.progress_bps_min}–{report.progress_locked.progress_bps_max} 锁定
            {" · "}
            sample_ok ≥ {report.sample_min_n}
          </p>
          <table className="shadow-compare-table">
            <thead>
              <tr>
                <th>指标</th>
                {columns.map((col) => (
                  <th key={col.id}>{col.id === "main" ? "主窗" : col.label}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              <tr>
                <th>n</th>
                {columns.map((col) => (
                  <td key={col.id}>{col.n}</td>
                ))}
              </tr>
              <tr>
                <th>胜率</th>
                {columns.map((col) => (
                  <td key={col.id}>{pct(col.win_rate)}</td>
                ))}
              </tr>
              <tr>
                <th>期望</th>
                {columns.map((col) => (
                  <td key={col.id}>{num(col.expectancy)}</td>
                ))}
              </tr>
              <tr>
                <th>中位 net bps</th>
                {columns.map((col) => (
                  <td key={col.id}>{bps(col.median_net_bps)}</td>
                ))}
              </tr>
              <tr>
                <th>sample_ok</th>
                {columns.map((col) => (
                  <td key={col.id}>{col.sample_ok ? "是" : `否（${col.n}/${report.sample_min_n}）`}</td>
                ))}
              </tr>
              <tr>
                <th>出场</th>
                {columns.map((col) => (
                  <td key={col.id}>{col.id === "main" ? exitMix(col) : vsMain(col.exit_vs_main) || exitMix(col)}</td>
                ))}
              </tr>
            </tbody>
          </table>
          {!report.sets.length ? <p className="muted tiny">无影子参数集。结果不会打开实盘。</p> : null}
        </>
      )}
    </div>
  );
}
