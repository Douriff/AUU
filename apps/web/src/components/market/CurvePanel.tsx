import type { CurveSnapshot } from "@/types/contracts";

interface Props {
  curve: CurveSnapshot | null;
}

function n(v: number | undefined, digits = 4): string {
  if (v == null || !Number.isFinite(v)) return "—";
  return v.toPrecision(digits);
}

export function CurvePanel({ curve }: Props) {
  if (!curve) {
    return (
      <div className="curve-panel">
        <div className="panel-title">Bonding curve</div>
        <p className="muted pad">等待 curve…</p>
      </div>
    );
  }
  const pct = curve.curve_progress != null ? Math.round(curve.curve_progress * 100) : 0;
  const status = curve.migrated ? "migrated" : curve.graduated ? "graduated" : "bonding";
  return (
    <div className="curve-panel">
      <div className="panel-title">Bonding curve · {curve.venue ?? "pump.fun"}</div>
      <div className="curve-body">
        <div className="curve-bar" aria-label={`curve ${pct}%`}>
          <div className="curve-bar-fill" style={{ width: `${pct}%` }} />
        </div>
        <dl>
          <dt>progress</dt>
          <dd>
            {pct}% · {status}
          </dd>
          <dt>virt SOL</dt>
          <dd>{n(curve.virtual_sol_reserves)}</dd>
          <dt>virt token</dt>
          <dd>{n(curve.virtual_token_reserves, 6)}</dd>
          <dt>price</dt>
          <dd>{n(curve.price_sol, 4)} SOL</dd>
        </dl>
      </div>
    </div>
  );
}
