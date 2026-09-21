import type { PumpfunPaperSnapshot } from "@/types/contracts";

interface Props {
  snapshot: PumpfunPaperSnapshot | null;
}

export function CurveProgressBar({ snapshot }: Props) {
  if (!snapshot) return null;
  const pct = Math.max(0, Math.min(100, snapshot.progress_bps / 100));
  const phase = snapshot.migrated ? "migrated" : snapshot.complete ? "graduating" : "curve";
  const label = snapshot.migrated
    ? "migrated"
    : snapshot.complete
      ? "complete"
      : `${pct.toFixed(1)}%`;

  return (
    <div className={`curve-progress phase-${phase}`} title={`${snapshot.mint} · ${snapshot.phase}`}>
      <span className="curve-progress-k">curve</span>
      <div className="curve-progress-bar" role="progressbar" aria-valuenow={pct} aria-valuemin={0} aria-valuemax={100}>
        <div className={`curve-progress-fill ${phase}`} style={{ width: `${pct}%` }} />
      </div>
      <span className="curve-progress-label">{label}</span>
      {snapshot.complete ? <span className="curve-chip">complete</span> : null}
      {snapshot.migrated ? <span className="curve-chip migrated">migrated</span> : null}
    </div>
  );
}
