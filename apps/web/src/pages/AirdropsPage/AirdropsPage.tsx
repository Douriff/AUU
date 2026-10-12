import { useCallback, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { marketProvider } from "@/providers/HttpWsProvider";
import { Empty, SkCards } from "@/components/ui/Skeleton";
import type { AirdropItem, AirdropStatus, AirdropsPage as AirdropsData, AirdropsQuery } from "@/types/mainstream";
import { useTranslation } from "react-i18next";
import { errText } from "@/i18n/errors";
import { fmtDate } from "@/i18n/format";

type Cat = "all" | "major" | "community" | "meme";

function bj(ms: number | null | undefined) {
  if (!ms) return "—";
  return fmtDate(ms, { timeZone: "Asia/Shanghai", month: "2-digit", day: "2-digit", year: "numeric" });
}

function statusClass(s: AirdropStatus) {
  if (s === "active") return "is-active";
  if (s === "upcoming") return "is-upcoming";
  return "is-ended";
}

function Card({ it }: { it: AirdropItem }) {
  const { t } = useTranslation();
  return (
    <article className={`ad-card ad-cat-${it.category}`}>
      <Link to={`/airdrops/${encodeURIComponent(it.id)}`} className="ad-card-link">
        <div className="ad-meta">
          <span className={`ad-badge ad-cat`}>{t(`airdrops.cat.${it.category}`)}</span>
          <span className={`ad-badge ad-st ${statusClass(it.status)}`}>{t(`airdrops.status.${it.status}`)}</span>
          <time dateTime={new Date(it.publishedAt).toISOString()}>{bj(it.publishedAt)}</time>
        </div>
        <h2 className="ad-title">{it.title}</h2>
        <p className="ad-project">{it.projectName}</p>
        {it.summary ? <p className="ad-sum">{it.summary}</p> : null}
      </Link>
      <div className="ad-foot">
        <span className="ad-src" title={t("airdrops.source")}>
          {t("airdrops.sourceLabel", { name: it.sourceName })}
        </span>
        {it.chains.length ? (
          <span className="ad-chains">
            {it.chains.slice(0, 4).map((c) => (
              <span key={c} className="ad-chip">
                {c}
              </span>
            ))}
          </span>
        ) : null}
        {it.sourceUrl ? (
          <a className="ad-ext" href={it.sourceUrl} target="_blank" rel="noopener noreferrer nofollow" onClick={(e) => e.stopPropagation()}>
            {t("airdrops.viewSource")} ›
          </a>
        ) : null}
      </div>
    </article>
  );
}

export function AirdropsPage() {
  const { t } = useTranslation();
  const [category, setCategory] = useState<Cat>("all");
  const [status, setStatus] = useState<"" | AirdropStatus>("");
  const [page, setPage] = useState(1);
  const [data, setData] = useState<AirdropsData | null>(null);
  const [err, setErr] = useState("");
  const [loading, setLoading] = useState(true);
  const topRef = useRef<HTMLDivElement | null>(null);
  const PAGE_SIZE = 20;

  const load = useCallback(() => {
    setLoading(true);
    const q: AirdropsQuery = {
      page,
      size: PAGE_SIZE,
      category,
      status: status || undefined,
    };
    return marketProvider
      .getAirdrops(q)
      .then((d) => {
        setData(d);
        setErr("");
      })
      .catch((e: unknown) => setErr(errText(e, "airdrops.cantConnect")))
      .finally(() => setLoading(false));
  }, [page, category, status]);

  useEffect(() => {
    void load();
  }, [load]);

  const pick = (fn: () => void) => {
    fn();
    setPage(1);
  };
  const go = (p: number) => {
    setPage(p);
    topRef.current?.scrollIntoView({ block: "start" });
  };
  const items = data?.items ?? [];

  return (
    <div className="pro-page airdrops-page" ref={topRef}>
      <header className="pro-head">
        <div>
          <h1>{t("airdrops.title")}</h1>
          <p>{t("airdrops.sub")}</p>
        </div>
      </header>

      <div className="ad-bar">
        <div className="seg" role="tablist" aria-label={t("airdrops.filterCat")}>
          {(
            [
              ["all", t("airdrops.cat.all")],
              ["major", t("airdrops.cat.major")],
              ["community", t("airdrops.cat.community")],
              ["meme", t("airdrops.cat.meme")],
            ] as [Cat, string][]
          ).map(([m, label]) => (
            <button key={m} type="button" role="tab" aria-selected={category === m} className={category === m ? "is-active" : ""} onClick={() => pick(() => setCategory(m))}>
              {label}
            </button>
          ))}
        </div>
        <div className="ad-chips" aria-label={t("airdrops.filterStatus")}>
          {(
            [
              ["", t("airdrops.statusAll")],
              ["active", t("airdrops.status.active")],
              ["upcoming", t("airdrops.status.upcoming")],
              ["ended", t("airdrops.status.ended")],
            ] as ["" | AirdropStatus, string][]
          ).map(([s, label]) => (
            <button key={s || "all"} type="button" className={`ad-chip-btn${status === s ? " is-active" : ""}`} onClick={() => pick(() => setStatus(s))}>
              {label}
            </button>
          ))}
        </div>
      </div>

      {err ? <div className="pro-alert">{t("airdrops.loadFailed", { err })}</div> : null}

      {loading && !data ? (
        <SkCards n={6} h={110} />
      ) : items.length === 0 ? (
        <Empty icon="search" title={t("airdrops.empty")} hint={t("airdrops.emptyHint")} />
      ) : (
        <div className="ad-list">
          {items.map((it) => (
            <Card key={it.id} it={it} />
          ))}
        </div>
      )}

      {data && data.pages > 1 ? (
        <nav className="news-pager" aria-label={t("airdrops.pager")}>
          <button type="button" className="btn-ghost" disabled={page <= 1} onClick={() => go(page - 1)}>
            ‹ {t("airdrops.prev")}
          </button>
          <span className="num">
            {page} / {data.pages}
          </span>
          <button type="button" className="btn-ghost" disabled={page >= data.pages} onClick={() => go(page + 1)}>
            {t("airdrops.next")} ›
          </button>
        </nav>
      ) : null}

      <footer className="ad-foot-note">
        <p className="muted">{data?.note ?? t("airdrops.disclaimer")}</p>
      </footer>
    </div>
  );
}
