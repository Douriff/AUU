import type { SymbolInfo } from "@/types/contracts";
import { truncateMint, VENUE } from "@/venue";

interface Props {
  symbols: SymbolInfo[];
  active: string;
  onSelect: (symbol: string) => void;
}

function pct(p?: number): string {
  if (p == null || !Number.isFinite(p)) return "—";
  return `${Math.round(p * 100)}%`;
}

export function SymbolList({ symbols, active, onSelect }: Props) {
  return (
    <div className="symbol-list">
      <div className="panel-title">自选 · {VENUE}</div>
      <ul>
        {symbols.map((s) => (
          <li key={s.symbol}>
            <button
              className={s.symbol === active ? "active" : ""}
              onClick={() => onSelect(s.symbol)}
              type="button"
            >
              <span className="sym">
                {s.base}/{s.quote}
              </span>
              <span className="kind">
                {s.graduated ? "graduated" : "bonding"} · {pct(s.curve_progress)}
              </span>
              <span className="mint">{truncateMint(s.mint ?? s.symbol)}</span>
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}
