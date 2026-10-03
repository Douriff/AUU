import type { CSSProperties } from "react";

/**
 * Decimal-point aligned number: integer part right-aligned in `int` ch, fraction left-aligned in `frac` ch.
 * Monospace (JetBrains Mono) so every digit has the same width.
 */
export function Num({
  text,
  int = 7,
  frac = 6,
  className = "",
  title,
}: {
  text: string;
  int?: number;
  frac?: number;
  className?: string;
  title?: string;
}) {
  const i = text.indexOf(".");
  const head = i < 0 ? text : text.slice(0, i);
  const tail = i < 0 ? "" : text.slice(i);
  const style = { "--ni": `${int}ch`, "--nd": `${frac}ch` } as CSSProperties;
  return (
    <span className={`dp ${className}`} style={style} title={title}>
      <span className="ni">{head}</span>
      <span className="nd">{tail}</span>
    </span>
  );
}
