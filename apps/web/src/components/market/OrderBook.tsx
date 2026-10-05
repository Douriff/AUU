import { useEffect, useMemo, useState } from "react";
import { fmtPx, pxWidths } from "@/components/market/MarketList";
import { Num } from "@/components/ui/Num";
import { Sk } from "@/components/ui/Skeleton";
import { marketProvider } from "@/providers/HttpWsProvider";
import type { MainstreamOrderbook } from "@/types/mainstream";
import { useTranslation } from "react-i18next";
import { fmtFixed } from "@/i18n/format";

export const BOOK_LEVELS = 12;
const POLL_MS = 3000;

function fmtQty(n: number): string {
  if (!Number.isFinite(n)) return "—";
  if (n >= 1e6) return `${fmtFixed(n / 1e6, 2)}M`;
  if (n >= 1e4) return `${fmtFixed(n / 1e3, 1)}K`;
  if (n >= 100) return fmtFixed(n, 1);
  if (n >= 1) return fmtFixed(n, 3);
  if (n >= 0.001) return fmtFixed(n, 4);
  return fmtFixed(n, 6);
}

function useNarrow(): boolean {
  const q = "(max-width: 900px)";
  const [narrow, setNarrow] = useState(() => typeof window !== "undefined" && window.matchMedia(q).matches);
  useEffect(() => {
    const m = window.matchMedia(q);
    const on = () => setNarrow(m.matches);
    m.addEventListener("change", on);
    return () => m.removeEventListener("change", on);
  }, []);
  return narrow;
}

type Row = { px: number; qty: number; cum: number };

function withCum(levels: [number, number][]): Row[] {
  let cum = 0;
  return levels.slice(0, BOOK_LEVELS).map(([px, qty]) => {
    cum += qty;
    return { px, qty, cum };
  });
}

/** Read-only spot order book (12 levels). Polls only while the tab is visible. */
export function OrderBook({ symbol, venue = null }: { symbol: string; venue?: string | null }) {
  const [book, setBook] = useState<MainstreamOrderbook | null>(null);
  const [err, setErr] = useState("");
  const narrow = useNarrow();

  useEffect(() => {
    let alive = true;
    let timer: number | undefined;
    setBook(null);
    setErr("");
    const load = async () => {
      window.clearTimeout(timer);
      if (document.hidden) return; // resumes on visibilitychange
      let next = POLL_MS;
      try {
        const b = await marketProvider.getMainstreamOrderbook(symbol, BOOK_LEVELS, venue);
        if (!alive) return;
        setBook(b);
        setErr("");
        // Still refreshing a stale snapshot after the server's bounded wait: ask again soon.
        if (b.pending || (b.stale && b.refreshing)) next = 800;
      } catch (e) {
        if (!alive) return;
        setErr(e instanceof Error ? e.message : String(e));
        next = POLL_MS * 3;
      }
      if (alive) timer = window.setTimeout(load, next);
    };
    const onVis = () => {
      if (!document.hidden) void load();
    };
    void load();
    document.addEventListener("visibilitychange", onVis);
    return () => {
      alive = false;
      window.clearTimeout(timer);
      document.removeEventListener("visibilitychange", onVis);
    };
  }, [symbol, venue]);

  const asks = useMemo(() => withCum(book?.asks ?? []), [book]);
  const bids = useMemo(() => withCum(book?.bids ?? []), [book]);
  const maxCum = Math.max(asks[asks.length - 1]?.cum ?? 0, bids[bids.length - 1]?.cum ?? 0) || 1;
  const w = useMemo(() => pxWidths([...asks, ...bids].map((r) => r.px)), [asks, bids]);
  const ready = asks.length > 0 && bids.length > 0;
  const base = symbol.toUpperCase();
  const { t } = useTranslation();
  const spread = book?.spreadBp != null ? `${fmtFixed(book.spreadBp, 2)} bp` : "—";

  const row = (r: Row, side: "ask" | "bid", key: number) => (
    <div key={key} className={`ob-row ob-${side}`}>
      <i className="ob-bar" style={{ width: `${(r.cum / maxCum) * 100}%` }} aria-hidden="true" />
      <span className={`ob-px ${side === "ask" ? "down" : "up"}`}>
        <Num text={fmtPx(r.px)} int={w.int} frac={w.frac} />
      </span>
      <span className="ob-qty num">{fmtQty(r.qty)}</span>
      <span className="ob-cum num dim">{fmtQty(r.cum)}</span>
    </div>
  );

  let body;
  if (book && !book.enabled) {
    body = <div className="ob-empty dim">{t("ob.closed")}</div>;
  } else if (!ready && (err || book?.error)) {
    body = <div className="ob-empty dim">{t("ob.unavailable")}</div>;
  } else if (!ready) {
    body = (
      <div className="ob-sk" role="status" aria-label={t("common.loading")}>
        {Array.from({ length: narrow ? 8 : 16 }, (_, i) => (
          <Sk key={i} h={10} w={`${55 + ((i * 17) % 40)}%`} />
        ))}
      </div>
    );
  } else if (narrow) {
    body = (
      <div className="ob-split">
        <div className="ob-col">
          <div className="ob-head dim"><span>{t("ob.bid")}</span><span>{t("ob.qty")}</span></div>
          {bids.map((r, i) => (
            <div key={i} className="ob-row ob-bid">
              <i className="ob-bar" style={{ width: `${(r.cum / maxCum) * 100}%` }} aria-hidden="true" />
              <span className="ob-px up num">{fmtPx(r.px)}</span>
              <span className="ob-qty num">{fmtQty(r.qty)}</span>
            </div>
          ))}
        </div>
        <div className="ob-col">
          <div className="ob-head dim"><span>{t("ob.ask")}</span><span>{t("ob.qty")}</span></div>
          {asks.map((r, i) => (
            <div key={i} className="ob-row ob-ask">
              <i className="ob-bar" style={{ width: `${(r.cum / maxCum) * 100}%` }} aria-hidden="true" />
              <span className="ob-px down num">{fmtPx(r.px)}</span>
              <span className="ob-qty num">{fmtQty(r.qty)}</span>
            </div>
          ))}
        </div>
      </div>
    );
  } else {
    body = (
      <>
        <div className="ob-head dim">
          <span>{t("ob.priceUnit", { q: "USDT" })}</span>
          <span>{t("ob.qtyUnit", { base })}</span>
          <span>{t("ob.cum")}</span>
        </div>
        <div className="ob-side ob-asks">{[...asks].reverse().map((r, i) => row(r, "ask", i))}</div>
        <div className="ob-mid">
          <b className="num">{fmtPx(book!.mid)}</b>
          <span className="dim num">{t("ob.spread")} {spread}</span>
        </div>
        <div className="ob-side ob-bids">{bids.map((r, i) => row(r, "bid", i))}</div>
      </>
    );
  }

  const age = book?.ageMs != null ? Math.max(0, Math.round(book.ageMs / 1000)) : null;
  return (
    <div className="ob" data-testid="orderbook">
      <div className="ph ob-ph">
        <span className="ob-title">{t("ob.title")}</span>
        <span className="tag ob-tag">{t("ob.displayOnly")}</span>
        <span className={`ob-age num ${book?.stale && ready ? "warn" : "dim"}`}>
          {book?.exchange ? `${book.exchange.toUpperCase()} · ` : ""}
          {book?.stale && book.refreshing && ready ? t("ob.refreshing") : age != null ? `${age}s` : "—"}
        </span>
      </div>
      <div className="ob-body">{body}</div>
      {narrow && ready && (
        <div className="ob-mid ob-mid-m">
          <b className="num">{fmtPx(book!.mid)}</b>
          <span className="dim num">{t("ob.spread")} {spread}</span>
        </div>
      )}
      <p className="ob-note">{t("ob.note")}</p>
    </div>
  );
}
