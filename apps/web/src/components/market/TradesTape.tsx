import type { TradeTick } from "@/types/contracts";

interface Props {
  trades: TradeTick[];
}

export function TradesTape({ trades }: Props) {
  return (
    <div className="trades-tape">
      <div className="panel-title">成交 Tape</div>
      <div className="tape-head">
        <span>时间</span>
        <span>价格</span>
        <span>数量</span>
      </div>
      <ul>
        {trades.map((t, i) => (
          <li key={`${t.ts}-${i}`} className={t.side}>
            <span>{new Date(t.ts).toLocaleTimeString()}</span>
            <span>{t.price.toPrecision(6)}</span>
            <span>{t.qty.toFixed(1)}</span>
          </li>
        ))}
        {!trades.length ? <li className="muted">等待 trades…</li> : null}
      </ul>
    </div>
  );
}
