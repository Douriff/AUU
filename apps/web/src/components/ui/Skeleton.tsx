import type { ReactNode } from "react";
import i18n from "i18next";

/** Shimmer placeholder block. */
export function Sk({ w = "100%", h = 12, r = 4, className = "" }: { w?: number | string; h?: number; r?: number; className?: string }) {
  return <span className={`sk ${className}`} style={{ width: w, height: h, borderRadius: r }} aria-hidden="true" />;
}

/** N placeholder rows shaped like a table/list. */
export function SkRows({ rows = 8, cols = 5, h = 40 }: { rows?: number; cols?: number; h?: number }) {
  return (
    <div className="sk-rows" role="status" aria-label={i18n.t("common.loading")}>
      {Array.from({ length: rows }, (_, i) => (
        <div key={i} className="sk-row" style={{ height: h }}>
          {Array.from({ length: cols }, (_, j) => (
            <Sk key={j} w={j === 0 ? "22%" : `${10 + ((i * 7 + j * 13) % 9)}%`} h={11} />
          ))}
        </div>
      ))}
    </div>
  );
}

/** Card-shaped skeleton grid (KPI tiles). */
export function SkCards({ n = 4, h = 72 }: { n?: number; h?: number }) {
  return (
    <div className="sk-cards" role="status" aria-label={i18n.t("common.loading")}>
      {Array.from({ length: n }, (_, i) => (
        <div key={i} className="sk-card" style={{ height: h }}>
          <Sk w="40%" h={10} />
          <Sk w="70%" h={18} />
        </div>
      ))}
    </div>
  );
}

/** Empty state: icon + title + optional hint/action. */
export function Empty({ title, hint, action, icon = "inbox" }: { title: string; hint?: ReactNode; action?: ReactNode; icon?: "inbox" | "search" | "chart" | "star" }) {
  const paths: Record<string, ReactNode> = {
    inbox: <path d="M4 13h4l1.5 2.5h5L16 13h4M5 13l2-7h10l2 7v5H5z" />,
    search: (
      <>
        <circle cx="11" cy="11" r="6" />
        <path d="m20 20-4.5-4.5" />
      </>
    ),
    chart: <path d="M4 19h16M7 16V10M12 16V6M17 16v-4" />,
    star: <path d="m12 4 2.4 5 5.4.7-4 3.8 1 5.4L12 16.3 7.2 19l1-5.4-4-3.8 5.4-.7z" />,
  };
  return (
    <div className="empty">
      <svg width="36" height="36" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinejoin="round" strokeLinecap="round" aria-hidden="true">
        {paths[icon]}
      </svg>
      <b>{title}</b>
      {hint ? <p>{hint}</p> : null}
      {action}
    </div>
  );
}
