import { useCallback, useEffect, useRef, useState } from "react";
import { marketProvider } from "@/providers/HttpWsProvider";
import { Empty, SkCards } from "@/components/ui/Skeleton";
import type { NewsItem, NewsPage as NewsData, NewsQuery } from "@/types/mainstream";
import { useTranslation } from "react-i18next";
import i18n from "i18next";
import { errText } from "@/i18n/errors";
import { fmtDate, fmtRelative } from "@/i18n/format";

/** 行业动态 (news): headlines/summaries stay in the original language (not translated); UI chrome is i18n. */
/** 行业动态: crypto headlines from public RSS (title + summary + original link, source named). Login required. */

const POLL_MS = 120_000;
const PAGE_SIZE = 20;
const TOP = ["BTC", "ETH", "SOL"];
type Mode = "all" | "focus" | "important";

/** Beijing time, formatted for the UI language. */
function bj(ms: number | null | undefined, withDate = true) {
  if (!ms) return "—";
  const hm = { timeZone: "Asia/Shanghai", hour: "2-digit", minute: "2-digit", hour12: false } as const;
  return fmtDate(ms, withDate ? { ...hm, month: "2-digit", day: "2-digit" } : hm);
}

function ago(ms: number, now: number) {
  const m = Math.max(0, Math.round((now - ms) / 60_000));
  if (m < 1) return i18n.t("news.justNow");
  if (m < 60 * 24) return fmtRelative(ms, now);
  return bj(ms);
}

function Item({ it, now, onCoin }: { it: NewsItem; now: number; onCoin: (c: string) => void }) {
  const { t } = useTranslation();
  return (
    <article className={`news-item${it.important ? " is-important" : ""}`}>
      <div className="news-meta">
        <span className="news-src">{it.sourceName}</span>
        <time dateTime={new Date(it.publishedAt).toISOString()} title={t("news.bjTime", { time: bj(it.publishedAt) })}>
          {ago(it.publishedAt, now)}
        </time>
        {it.important ? <span className="news-hot">{t("news.important")}</span> : null}
      </div>
      <h2 className="news-title" lang="en" dir="ltr">
        <a href={it.url} target="_blank" rel="noopener noreferrer nofollow">
          {it.title}
        </a>
      </h2>
      {it.summary ? <p className="news-sum" lang="en" dir="ltr">{it.summary}</p> : null}
      <div className="news-foot">
        {it.coins.map((c) => (
          <button key={c} type="button" className={`news-coin${TOP.includes(c) ? " is-top" : ""}`} onClick={() => onCoin(c)} title={t("news.onlyCoin", { c })}>
            {c}
          </button>
        ))}
        <a className="news-link" href={it.url} target="_blank" rel="noopener noreferrer nofollow">
          {t("news.readOriginal")} · {it.sourceName} ›
        </a>
      </div>
    </article>
  );
}

export function NewsPage() {
  const { t } = useTranslation();
  const [mode, setMode] = useState<Mode>("all");
  const [coin, setCoin] = useState("");
  const [source, setSource] = useState("");
  const [page, setPage] = useState(1);
  const [data, setData] = useState<NewsData | null>(null);
  const [err, setErr] = useState("");
  const [loading, setLoading] = useState(true);
  const [checkedAt, setCheckedAt] = useState(0);
  const [now, setNow] = useState(() => Date.now());
  const topRef = useRef<HTMLDivElement | null>(null);

  const load = useCallback(
    (quiet = false) => {
      if (!quiet) setLoading(true);
      const q: NewsQuery = { page, size: PAGE_SIZE, coin: coin || undefined, source: source || undefined, focus: mode === "focus", important: mode === "important" };
      return marketProvider
        .getNews(q)
        .then((d) => {
          setData(d);
          setErr("");
          setCheckedAt(Date.now());
        })
        .catch((e: unknown) => setErr(errText(e, "news.cantConnect")))
        .finally(() => {
          setLoading(false);
          setNow(Date.now());
        });
    },
    [page, coin, source, mode],
  );

  useEffect(() => {
    void load();
  }, [load]);

  // auto refresh (only while the tab is visible); the server fetches its feeds every ~12 min
  useEffect(() => {
    const timer = window.setInterval(() => {
      if (!document.hidden) void load(true);
    }, POLL_MS);
    const onVis = () => {
      if (!document.hidden) void load(true);
    };
    document.addEventListener("visibilitychange", onVis);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", onVis);
    };
  }, [load]);

  const pick = (fn: () => void) => {
    fn();
    setPage(1);
  };
  const go = (p: number) => {
    setPage(p);
    topRef.current?.scrollIntoView({ block: "start" });
  };
  const coins = data?.coins ?? TOP;
  const items = data?.items ?? [];
  const failing = (data?.sources ?? []).filter((s) => !s.ok && s.lastAttemptAt);

  return (
    <div className="pro-page news-page" ref={topRef}>
      <header className="pro-head">
        <div>
          <h1>{t("news.title")}</h1>
          <p>{t("news.sub")}</p>
        </div>
        <span className="news-upd muted" title={t("news.updTitle")}>
          {checkedAt ? t("news.updated", { time: bj(checkedAt, false) }) : t("common.loadingDots")}
        </span>
      </header>

      <div className="news-bar">
        <div className="seg" role="tablist" aria-label={t("news.filter")}>
          {(
            [
              ["all", t("ml.all")],
              ["focus", t("news.focus")],
              ["important", t("news.important")],
            ] as [Mode, string][]
          ).map(([m, label]) => (
            <button key={m} type="button" role="tab" aria-selected={mode === m} className={mode === m ? "is-active" : ""} onClick={() => pick(() => setMode(m))}>
              {label}
            </button>
          ))}
        </div>
        <div className="news-chips" aria-label={t("ms.col.coin")}>
          {TOP.map((c) => (
            <button key={c} type="button" className={`news-chip${coin === c ? " is-active" : ""}`} onClick={() => pick(() => setCoin(coin === c ? "" : c))}>
              {c}
            </button>
          ))}
          <select
            className="news-select"
            aria-label={t("news.moreCoins")}
            value={TOP.includes(coin) ? "" : coin}
            onChange={(e) => pick(() => setCoin(e.target.value))}
          >
            <option value="">{t("news.moreCoins")}</option>
            {coins
              .filter((c) => !TOP.includes(c))
              .map((c) => (
                <option key={c} value={c}>
                  {c}
                </option>
              ))}
          </select>
          <select className="news-select" aria-label={t("news.source")} value={source} onChange={(e) => pick(() => setSource(e.target.value))}>
            <option value="">{t("news.allSources")}</option>
            {(data?.sources ?? []).map((s) => (
              <option key={s.id} value={s.id}>
                {s.name}
              </option>
            ))}
          </select>
          {coin || source || mode !== "all" ? (
            <button type="button" className="news-clear" onClick={() => pick(() => (setCoin(""), setSource(""), setMode("all")))}>
              {t("news.clear")}
            </button>
          ) : null}
        </div>
      </div>

      {err ? <div className="pro-alert">{t("news.loadFailed", { err })}</div> : null}
      {failing.length ? (
        <div className="pro-alert is-warn">
          {t("news.failing", { names: failing.map((s) => s.name).join(", ") })}
        </div>
      ) : null}

      {loading && !data ? (
        <SkCards n={6} h={96} />
      ) : items.length === 0 ? (
        <Empty
          icon="search"
          title={data && data.total === 0 && !coin && !source && mode === "all" ? t("news.emptyAll") : t("news.emptyFilter")}
          hint={data && data.total === 0 && !coin && !source && mode === "all" ? t("news.emptyAllHint") : t("news.emptyFilterHint")}
        />
      ) : (
        <div className="news-list">
          {items.map((it) => (
            <Item key={it.id} it={it} now={now} onCoin={(c) => pick(() => setCoin(c))} />
          ))}
        </div>
      )}

      {data && data.pages > 1 ? (
        <nav className="news-pager" aria-label={t("news.pager")}>
          <button type="button" className="btn-ghost" disabled={page <= 1} onClick={() => go(page - 1)}>
            ‹ {t("news.prev")}
          </button>
          <span className="num">
            {page} / {data.pages}
          </span>
          <button type="button" className="btn-ghost" disabled={page >= data.pages} onClick={() => go(page + 1)}>
            {t("news.next")} ›
          </button>
        </nav>
      ) : null}

      <footer className="news-src-foot">
        <div className="news-src-list">
          {t("news.sources")}
          {(data?.sources ?? []).map((s) => (
            <span key={s.id} className="news-src-st">
              <i className={`st-dot ${s.ok ? "ok" : s.lastAttemptAt ? "bad" : ""}`} aria-hidden="true" />
              <a href={s.home} target="_blank" rel="noopener noreferrer nofollow">
                {s.name}
              </a>
              <span className="muted">{s.lastOkAt ? ` ${t("news.srcUpdated", { time: bj(s.lastOkAt) })}` : ` ${t("news.srcWaiting")}`}</span>
            </span>
          ))}
        </div>
        <p className="muted">{t("news.copyright")} {t("news.originalNote")}</p>
      </footer>
    </div>
  );
}
