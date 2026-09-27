import { useState } from "react";
import { usePostmortem } from "@/hooks/usePostmortem";
import type { PostmortemExitBucket, PostmortemImpactBucket, PostmortemReport } from "@/types/contracts";

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

function ExitBars({ rows }: { rows: PostmortemExitBucket[] }) {
  const active = rows.filter((r) => r.count > 0);
  const max = Math.max(...active.map((r) => r.pct), 1e-9);
  if (!active.length) return <p className="muted tiny">暂无出场分布</p>;
  return (
    <div className="pm-exit-bars" aria-label="exit reason mix">
      {active.map((r) => (
        <label key={r.reason}>
          <span>{EXIT_LABEL[r.reason] ?? r.reason}</span>
          <i style={{ width: `${Math.max(4, (r.pct / max) * 100)}%` }} />
          <em>
            {pct(r.pct)} · {r.count}
          </em>
        </label>
      ))}
    </div>
  );
}

function ImpactTable({ rows }: { rows: PostmortemImpactBucket[] }) {
  const [open, setOpen] = useState(true);
  return (
    <div className="pm-impact">
      <button type="button" className="ghost tiny" onClick={() => setOpen((v) => !v)}>
        冲击分位 {open ? "▴" : "▾"}
      </button>
      {open ? (
        <table className="pm-impact-table">
          <thead>
            <tr>
              <th>Q</th>
              <th>n</th>
              <th>期望</th>
              <th>中位冲击</th>
              <th>主出场</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.q}>
                <td>{r.q}</td>
                <td>{r.count}</td>
                <td>{num(r.expectancy)}</td>
                <td>{r.median_impact_bps == null ? "—" : `${r.median_impact_bps.toFixed(1)} bps`}</td>
                <td>{r.dominant_exit ? EXIT_LABEL[r.dominant_exit] ?? r.dominant_exit : "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : null}
    </div>
  );
}

function Summary({ report }: { report: PostmortemReport }) {
  const s = report.summary;
  const go = Boolean(report.exec?.gates?.overall_go);
  return (
    <div className="pm-summary">
      <dl>
        <div>
          <dt>平仓</dt>
          <dd>
            {s.n_closed}
            <span className={`sample-badge ${s.sample_ok ? "ok" : ""}`}>
              {s.sample_ok ? "sample_ok" : `累积中（${s.n_closed}/30）`}
            </span>
          </dd>
        </div>
        <div>
          <dt>期望</dt>
          <dd>{num(s.expectancy)}</dd>
        </div>
        <div>
          <dt>胜率</dt>
          <dd>{pct(s.win_rate)}</dd>
        </div>
        <div>
          <dt>Exec</dt>
          <dd className={go ? "ok" : "warn"}>{go ? "Go" : "NoGo"}</dd>
        </div>
      </dl>
      {!go ? <p className="muted tiny">Go 未过 · 不展示实盘入口文案</p> : null}
    </div>
  );
}

interface Props {
  compact?: boolean;
  pollMs?: number;
}

export function PostmortemPanel({ compact, pollMs = 0 }: Props) {
  const { report, err, loading, refresh } = usePostmortem(pollMs);
  const findings = (report?.findings ?? []).slice(0, 5);

  return (
    <div className={`postmortem-panel ${compact ? "compact" : ""}`} aria-label="paper postmortem">
      <header>
        <h3>平仓后推演</h3>
        <span className="muted tiny">规则聚合 · 非大模型</span>
        <button type="button" className="ghost tiny" onClick={() => void refresh()} disabled={loading}>
          {loading ? "刷新中…" : "刷新"}
        </button>
      </header>
      {err ? <p className="error tiny">{err}</p> : null}
      {!report ? (
        <p className="muted tiny">加载推演…</p>
      ) : (
        <>
          <Summary report={report} />
          <ExitBars rows={report.by_exit_reason ?? []} />
          <ImpactTable rows={report.by_impact_quartile ?? []} />
          <ul className="pm-findings">
            {findings.length === 0 ? (
              <li className="muted tiny">暂无规则结论</li>
            ) : (
              findings.map((f) => (
                <li key={f.id} data-severity={f.severity}>
                  <strong>{f.title}</strong>
                  <span className="muted"> {f.detail}</span>
                </li>
              ))
            )}
          </ul>
          {report.scenario?.enabled ? <p className="muted tiny">仅动能对照，不改 progress</p> : null}
        </>
      )}
    </div>
  );
}
