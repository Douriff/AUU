import { memo } from "react";

interface Props {
  points: number[];
}

export const Sparkline = memo(function Sparkline({ points }: Props) {
  const w = 88;
  const h = 32;
  const series = points.filter((p) => Number.isFinite(p));
  if (series.length < 2) {
    return <svg className="mk-spark" width={w} height={h} viewBox={`0 0 ${w} ${h}`} aria-hidden="true" />;
  }
  const min = Math.min(...series);
  const max = Math.max(...series);
  const span = max - min || Math.abs(max) * 0.02 || 1e-12;
  const up = series[series.length - 1] >= series[0];
  const color = up ? "#3ddc84" : "#ff5d5d";
  const d = series
    .map((p, i) => {
      const x = (i / (series.length - 1)) * (w - 4) + 2;
      const y = h - 3 - ((p - min) / span) * (h - 6);
      return `${i === 0 ? "M" : "L"}${x.toFixed(2)} ${y.toFixed(2)}`;
    })
    .join(" ");
  return (
    <svg className="mk-spark" width={w} height={h} viewBox={`0 0 ${w} ${h}`} aria-hidden="true">
      <path d={d} fill="none" stroke={color} strokeWidth="1.7" strokeLinejoin="round" strokeLinecap="round" />
    </svg>
  );
});
