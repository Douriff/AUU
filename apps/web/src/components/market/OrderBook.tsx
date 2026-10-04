import { useEffect, useMemo, useState } from "react";
import { fmtPx, pxWidths } from "@/components/market/MarketList";
import { Num } from "@/components/ui/Num";
import { Sk } from "@/components/ui/Skeleton";
import { marketProvider } from "@/providers/HttpWsProvider";
import type { MainstreamOrderbook } from "@/types/mainstream";

export const BOOK_LEVELS = 12;
export const BOOK_NOTE = "仅展示，纸面按行情价±模拟滑点成交";
const POLL_MS = 3000;

function fmtQty(n: number): string {
  if (!Number.isFinite(n)) return "—";
  if (n >= 1e6) return `${(n / 1e6).toFixed(2)}M`;
  if (n >= 1e4) return `${(n / 1e3).toFixed(1)}K`;
  if (n >= 100) return n.toFixed(1);
  if (n >= 1) return n.toFixed(3);
  if (n >= 0.001) return n.toFixed(4);
  return n.toFixed(6);
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
    body = <div className="ob-empty dim">订单簿已关闭</div>;
  } else if (!ready && (err || book?.error)) {
    body = <div className="ob-empty dim">订单簿暂不可用</div>;
  } else if (!ready) {
    body = (
      <div className="ob-sk" role="status" aria-label="加载中">
        {Array.from({ length: narrow ? 8 : 16 }, (_, i) => (
          <Sk key={i} h={10} w={`${55 + ((i * 17) % 40)}%`} />
        ))}
      </div>
    );
  } else if (narrow) {
    body = (
      <div className="ob-split">
        <div className="ob-col">
          <div className="ob-head dim"><span>买价</span><span>数量</span></div>
          {bids.map((r, i) => (
            <div key={i} className="ob-row ob-bid">
              <i className="ob-bar" style={{ width: `${(r.cum / maxCum) * 100}%` }} aria-hidden="true" />
              <span className="ob-px up num">{fmtPx(r.px)}</span>
              <span className="ob-qty num">{fmtQty(r.qty)}</span>
            </div>
          ))}
        </div>
        <div className="ob-col">
          <div className="ob-head dim"><span>卖价</span><span>数量</span></div>
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
          <span>价格(USDT)</span>
          <span>数量({base})</span>
          <span>累计</span>
        </div>
        <div className="ob-side ob-asks">{[...asks].reverse().map((r, i) => row(r, "ask", i))}</div>
        <div className="ob-mid">
          <b className="num">{fmtPx(book!.mid)}</b>
          <span className="dim num">价差 {book!.spreadBp != null ? `${book!.spreadBp.toFixed(2)} bp` : "—"}</span>
        </div>
        <div className="ob-side ob-bids">{bids.map((r, i) => row(r, "bid", i))}</div>
      </>
    );
  }

  const age = book?.ageMs != null ? Math.max(0, Math.round(book.ageMs / 1000)) : null;
  return (
    <div className="ob" data-testid="orderbook">
      <div className="ph ob-ph">
        <span className="ob-title">订单簿</span>
        <span className="tag ob-tag">仅展示</span>
        <span className={`ob-age num ${book?.stale && ready ? "warn" : "dim"}`}>
          {book?.exchange ? `${book.exchange.toUpperCase()} · ` : ""}
          {book?.stale && book.refreshing && ready ? "刷新中" : age != null ? `${age}s` : "—"}
        </span>
      </div>
      <div className="ob-body">{body}</div>
      {narrow && ready && (
        <div className="ob-mid ob-mid-m">
          <b className="num">{fmtPx(book!.mid)}</b>
          <span className="dim num">价差 {book!.spreadBp != null ? `${book!.spreadBp.toFixed(2)} bp` : "—"}</span>
        </div>
      )}
      <p className="ob-note">{BOOK_NOTE}</p>
    </div>
  );
}
