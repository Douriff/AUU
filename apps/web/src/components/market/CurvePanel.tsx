import type { PumpfunPaperSnapshot } from "@/types/contracts";

interface Props {
  snapshot: PumpfunPaperSnapshot | null;
}

function n(v: number | undefined, digits = 4): string {
  if (v == null || !Number.isFinite(v)) return "—";
  return v.toPrecision(digits);
}

function lamportsToSol(raw: string | undefined): number | undefined {
  if (!raw) return undefined;
  const v = Number(raw);
  if (!Number.isFinite(v)) return undefined;
  return v / 1e9;
}

function tokensUi(raw: string | undefined): number | undefined {
  if (!raw) return undefined;
  const v = Number(raw);
  if (!Number.isFinite(v)) return undefined;
  return v / 1e6;
}

export function CurvePanel({ snapshot }: Props) {
  if (!snapshot) {
    return (
      <div className="curve-panel">
        <div className="panel-title">Bonding curve</div>
        <p className="muted pad">等待 curve…</p>
      </div>
    );
  }
  const pct = Math.max(0, Math.min(100, snapshot.progress_bps / 100));
  const status = snapshot.migrated ? "migrated" : snapshot.complete ? "graduating" : "bonding";
  return (
    <div className="curve-panel">
      <div className="panel-title">Bonding curve · Pump.fun</div>
      <div className="curve-body">
        <div className="curve-bar" aria-label={`curve ${pct.toFixed(1)}%`}>
          <div className="curve-bar-fill" style={{ width: `${pct}%` }} />
        </div>
        <dl>
          <dt>progress</dt>
          <dd>
            {pct.toFixed(1)}% · {status}
          </dd>
          <dt>virt SOL</dt>
          <dd>{n(lamportsToSol(snapshot.virtual_sol_reserves))}</dd>
          <dt>virt token</dt>
          <dd>{n(tokensUi(snapshot.virtual_token_reserves), 6)}</dd>
          <dt>price</dt>
          <dd>{n(snapshot.price_sol, 4)} SOL</dd>
        </dl>
      </div>
    </div>
  );
}
