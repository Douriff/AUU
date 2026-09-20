import type { BookSnapshot } from "@/types/contracts";

interface Props {
  book: BookSnapshot | null;
}

export function DepthPanel({ book }: Props) {
  return (
    <div className="depth-panel">
      <div className="panel-title">
        深度 Depth
        {book ? (
          <span className="muted"> spread {book.spread_bps.toFixed(1)} bps</span>
        ) : null}
      </div>
      {!book ? (
        <div className="muted pad">等待 book…</div>
      ) : (
        <div className="depth-grid">
          <div className="asks">
            {[...book.asks].reverse().map((l, i) => (
              <div key={`a-${i}`} className="row ask">
                <span>{l.price.toPrecision(6)}</span>
                <span>{l.size.toFixed(1)}</span>
              </div>
            ))}
          </div>
          <div className="mid">mid {book.mid.toPrecision(6)}</div>
          <div className="bids">
            {book.bids.map((l, i) => (
              <div key={`b-${i}`} className="row bid">
                <span>{l.price.toPrecision(6)}</span>
                <span>{l.size.toFixed(1)}</span>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
