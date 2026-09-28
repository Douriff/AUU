import type { BoardEquityPoint } from "@/types/contracts";

interface Props {
  points: BoardEquityPoint[];
  positive: boolean;
}

const W = 640;
const H = 196;

export function EquityCurve({ points, positive }: Props) {
  const color = positive ? "#3ee08f" : "#ff5d5d";
  const padL = 4;
  const padR = 4;
  const padT = 10;
  const padB = 8;
  const innerW = W - padL - padR;
  const innerH = H - padT - padB;

  const series = points.length ? points : [{ t: 0, pnl: 0 }, { t: 1, pnl: 0 }];
  const ys = series.map((p) => p.pnl);
  let min = Math.min(0, ...ys);
  let max = Math.max(0, ...ys);
  if (max - min < 1e-9) {
    min -= 1;
    max += 1;
  }
  const span = max - min || 1;
  const xOf = (i: number) =>
    series.length === 1 ? padL + innerW / 2 : padL + (i / (series.length - 1)) * innerW;
  const yOf = (v: number) => padT + (1 - (v - min) / span) * innerH;

  const line = series
    .map((p, i) => `${i === 0 ? "M" : "L"}${xOf(i).toFixed(2)} ${yOf(p.pnl).toFixed(2)}`)
    .join(" ");
  const lastX = xOf(series.length - 1);
  const firstX = xOf(0);
  const baseY = yOf(0);
  const area = `${line} L${lastX.toFixed(2)} ${baseY.toFixed(2)} L${firstX.toFixed(2)} ${baseY.toFixed(2)} Z`;

  const grids = [0, 1, 2, 3].map((i) => {
    const y = padT + (i / 3) * innerH;
    return y;
  });

  return (
    <svg
      className="board-equity"
      viewBox={`0 0 ${W} ${H}`}
      role="img"
      aria-label={points.length ? "累计净盈亏曲线" : "尚无已平仓，权益曲线持平"}
      preserveAspectRatio="xMidYMid meet"
    >
      <defs>
        <linearGradient id="board-equity-fill" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor={color} stopOpacity="0.28" />
          <stop offset="100%" stopColor={color} stopOpacity="0" />
        </linearGradient>
      </defs>
      {grids.map((y) => (
        <line
          key={y}
          x1={padL}
          x2={W - padR}
          y1={y}
          y2={y}
          stroke="rgba(255,255,255,0.06)"
          strokeWidth="1"
        />
      ))}
      <line
        x1={padL}
        x2={W - padR}
        y1={baseY}
        y2={baseY}
        stroke="rgba(255,255,255,0.12)"
        strokeDasharray="3 4"
        strokeWidth="1"
      />
      {points.length ? <path d={area} fill="url(#board-equity-fill)" stroke="none" /> : null}
      <path d={line} fill="none" stroke={color} strokeWidth="2.25" strokeLinejoin="round" strokeLinecap="round" />
      {points.length ? (
        <circle cx={lastX} cy={yOf(series[series.length - 1].pnl)} r="3.5" fill={color} />
      ) : null}
    </svg>
  );
}
