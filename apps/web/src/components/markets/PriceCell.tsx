import { memo, useEffect, useRef, useState } from "react";

function formatPx(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n)) return "—";
  const a = Math.abs(n);
  if (a === 0) return "0";
  if (a >= 1000) return n.toLocaleString("en-US", { maximumFractionDigits: 2 });
  const digits = a >= 1 ? 4 : a >= 0.01 ? 4 : a >= 0.0001 ? 6 : a >= 0.000001 ? 8 : 0;
  if (!digits) return n.toExponential(2);
  return n
    .toFixed(digits)
    .replace(/(\.\d*?[1-9])0+$/, "$1")
    .replace(/\.0+$/, "");
}

interface Props {
  price: number | null;
}

/** Updates in place and flashes green or red when the SOL price ticks. */
export const PriceCell = memo(function PriceCell({ price }: Props) {
  const prev = useRef<number | null>(null);
  const [flash, setFlash] = useState<"" | "up" | "down" | "idle">("");

  useEffect(() => {
    const before = prev.current;
    prev.current = price;
    if (before == null || price == null || price === before) return;
    const dir = price > before ? "up" : "down";
    setFlash("idle");
    const raf = window.requestAnimationFrame(() => setFlash(dir));
    const timer = window.setTimeout(() => setFlash(""), 780);
    return () => {
      window.cancelAnimationFrame(raf);
      window.clearTimeout(timer);
    };
  }, [price]);

  const tone = flash === "up" || flash === "down" ? flash : "";
  return <span className={`mk-price${tone ? ` flash-${tone}` : ""}`}>{formatPx(price)}</span>;
});
