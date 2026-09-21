import type { ExecutabilityReport } from "@/types/contracts";

function num(n: number | null | undefined, digits = 4): string {
  if (n == null || !Number.isFinite(n)) return "—";
  return n.toPrecision(digits);
}

function bps(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n)) return "—";
  return `${n.toFixed(1)} bps`;
}

function ratePct(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n)) return "—";
  return `${(n * 100).toFixed(1)}%`;
}

function RejectBars({
  progress,
  impact,
  risk,
}: {
  progress: number;
  impact: number;
  risk: number;
}) {
  const max = Math.max(progress, impact, risk, 1e-9);
  const rows = [
    { key: "progress", label: "进度", v: progress },
    { key: "impact", label: "冲击", v: impact },
    { key: "risk", label: "风控", v: risk },
  ];
  return (
    <div className="exec-bars" aria-label="reject buckets">
      {rows.map((r) => (
        <label key={r.key}>
          <span>{r.label}</span>
          <i style={{ width: `${Math.max(4, (r.v / max) * 100)}%` }} />
          <em>{ratePct(r.v)}</em>
        </label>
      ))}
    </div>
  );
}

interface Props {
  report: ExecutabilityReport | null;
  err?: string;
  compact?: boolean;
}

export function ExecutabilityPanel({ report, err, compact }: Props) {
  const lamp = report?.lamp ?? "gray";
  const verdict = report?.verdict === "go" ? "go" : "nogo";
  const n = report?.n_closed ?? report?.n_trades ?? 0;
  const sampleOk = Boolean(report?.sample_ok);
  const rr = report?.reject_rate;
  const live = report?.live_checks;

  return (
    <div className={`exec-panel ${compact ? "compact" : ""}`} aria-label="executability evidence">
      <header>
        <h3>可执行性证据</h3>
        <span className={`exec-lamp ${lamp}`} title="Go/NoGo" />
        <span className={`exec-verdict ${verdict}`}>
          {report?.verdict === "go" ? "Go" : "NoGo"}
        </span>
        <span className="muted tiny">实盘仍关 · liveEnabled=false</span>
      </header>
      {err ? <p className="error tiny">{err}</p> : null}
      {report ? (
        <>
          <p className="exec-nogo tiny">{report.nogo_reason || "证据不足"}</p>
          <dl>
            <div>
              <dt>平仓样本</dt>
              <dd>
                {n}
                <span className={`sample-badge ${sampleOk ? "ok" : ""}`}>
                  {sampleOk ? "sample_ok" : "不足"}
                </span>
                <span className="muted"> ≥30</span>
              </dd>
            </div>
            <div>
              <dt>期望</dt>
              <dd className={report.gates?.expectancy?.warn_negative ? "warn" : undefined}>
                {num(report.expectancy)}
                {report.gates?.expectancy?.warn_negative ? (
                  <span className="muted"> 微负未书面接受</span>
                ) : null}
              </dd>
            </div>
            <div>
              <dt>入场冲击中位</dt>
              <dd>
                {bps(report.median_entry_impact_bps)}
                <span className="muted"> &lt;60 / 硬顶 {report.hard_max_impact_bps}</span>
              </dd>
            </div>
            <div>
              <dt>影子滑点</dt>
              <dd>
                P50 {bps(report.shadow_slippage?.p50_bps ?? report.shadow_slippage?.median_bps)}
                {" · "}
                P90 {bps(report.shadow_slippage?.p90_bps)}
                <span className="muted"> ≤{report.shadow_slippage?.x_bps ?? 40}</span>
              </dd>
            </div>
            <div>
              <dt>冲击误差</dt>
              <dd>
                P50 {bps(report.impact_error?.p50_bps)}
                {" · "}
                P90 {bps(report.impact_error?.p90_bps)}
              </dd>
            </div>
          </dl>
          <RejectBars
            progress={rr?.progress?.rate ?? rr?.progress_band?.rate ?? 0}
            impact={rr?.impact?.rate ?? 0}
            risk={rr?.risk?.rate ?? 0}
          />
          <ul className="exec-live-checks">
            <li data-on={live?.live_limits ? "1" : "0"}>限额 LiveLimits</li>
            <li data-on={live?.keypair_mounted ? "1" : "0"}>本机钥</li>
            <li data-on={live?.secondary_confirm ? "1" : "0"}>二次确认</li>
          </ul>
          {!compact ? (
            <p className="muted tiny exec-note">
              开 live 前的纸面证据，不是跟单，也不是收益承诺。二次确认 + LiveLimits 之前不会开实盘。
            </p>
          ) : null}
        </>
      ) : (
        <p className="muted tiny">加载可执行性…</p>
      )}
    </div>
  );
}
