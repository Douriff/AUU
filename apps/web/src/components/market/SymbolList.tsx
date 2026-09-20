import type { SymbolInfo } from "@/types/contracts";

interface Props {
  symbols: SymbolInfo[];
  active: string;
  onSelect: (symbol: string) => void;
}

export function SymbolList({ symbols, active, onSelect }: Props) {
  return (
    <div className="symbol-list">
      <div className="panel-title">自选 Watchlist</div>
      <ul>
        {symbols.map((s) => (
          <li key={s.symbol}>
            <button
              className={s.symbol === active ? "active" : ""}
              onClick={() => onSelect(s.symbol)}
              type="button"
            >
              <span className="sym">{s.symbol}</span>
              <span className="kind">{s.kind ?? "meme_mock"}</span>
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}
