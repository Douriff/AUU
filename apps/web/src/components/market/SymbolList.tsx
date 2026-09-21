import type { SymbolInfo } from "@/types/contracts";
import { truncateMint, VENUE } from "@/venue";

interface Props {
  symbols: SymbolInfo[];
  active: string;
  onSelect: (symbol: string) => void;
}

export function SymbolList({ symbols, active, onSelect }: Props) {
  const pump = symbols.some((s) => s.kind === "pumpfun_curve" || Boolean(s.mint));
  return (
    <div className="symbol-list">
      <div className="panel-title">自选{pump ? ` · ${VENUE}` : ""}</div>
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
              <span className="kind">{s.kind ?? "spot"}</span>
              {s.mint ? <span className="mint">{truncateMint(s.mint)}</span> : null}
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}
