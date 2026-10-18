import { useCallback, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { marketProvider } from "@/providers/HttpWsProvider";
import { Empty, SkCards } from "@/components/ui/Skeleton";
import type { AirdropItem, AirdropStatus, AirdropVerification, AirdropsPage as AirdropsData, AirdropsQuery } from "@/types/mainstream";
import { useTranslation } from "react-i18next";
import { errText } from "@/i18n/errors";
import { fmtDate, fmtRelative } from "@/i18n/format";
import { AUTO_REFRESH_MS, createAutoRefresh, degradedFeeds, minutesAgo, serverClock, staleMinutes } from "./refresh";

type Cat = "all" | "major" | "community" | "meme";

function bj(ms: number | null | undefined) {
  if (!ms) return "—";
  return fmtDate(ms, { timeZone: "Asia/Shanghai", month: "2-digit", day: "2-digit", year: "numeric" });
}

/** "刚刚" under a minute, else a localized relative time ("3 分钟前"). */
function relWhen(t: (k: string) => string, thenMs: number, nowMs: number) {
  return minutesAgo(thenMs, nowMs) < 1 ? t("airdrops.justNow") : fmtRelative(thenMs, nowMs);
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
          {it.origin === "ingested" ? <span className="ad-badge ad-ing">{t("airdrops.origin.ingested")}</span> : null}
          <span className={`ad-badge ad-ver is-${it.verification}`} title={t(`airdrops.verifyTip.${it.verification}`)}>
            {t(`airdrops.verify.${it.verification}`)}
          </span>
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
        <span className="ad-srcn">{t("airdrops.sourceCount", { n: it.sourceCount })}</span>
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
      <p className="ad-risk" role="note">
        <span aria-hidden="true">⚠ </span>
        {t("airdrops.riskShort")}
      </p>
    </article>
  );
}

export function AirdropsPage() {
  const { t } = useTranslation();
  const [category, setCategory] = useState<Cat>("all");
  const [status, setStatus] = useState<"" | AirdropStatus>("");
  const [originFilter, setOriginFilter] = useState<"" | "curated" | "ingested">("");
  const [verifyFilter, setVerifyFilter] = useState<"" | AirdropVerification>("");
  const [page, setPage] = useState(1);
  const [data, setData] = useState<AirdropsData | null>(null);
  const [err, setErr] = useState("");
  const [refreshErr, setRefreshErr] = useState(false);
  const [loading, setLoading] = useState(true);
  const [fetchedAt, setFetchedAt] = useState(0);
  const [now, setNow] = useState(() => Date.now());
  const topRef = useRef<HTMLDivElement | null>(null);
  const reqSeq = useRef(0);
  const inflight = useRef(false);
  const PAGE_SIZE = 20;

  // quiet = background refresh: no skeleton, keeps the current list on failure (shows a gentle note instead)
  const load = useCallback(
    (quiet = false) => {
      if (quiet && inflight.current) return Promise.resolve();
      if (!quiet) setLoading(true);
      const seq = ++reqSeq.current; // a newer filter/page request supersedes an older response
      inflight.current = true;
      const q: AirdropsQuery = {
        page,
        size: PAGE_SIZE,
        category,
        status: status || undefined,
        origin: originFilter || undefined,
        verification: verifyFilter || undefined,
      };
      return marketProvider
        .getAirdrops(q)
        .then((d) => {
          if (seq !== reqSeq.current) return;
          setData(d);
          setErr("");
          setRefreshErr(false);
          setFetchedAt(Date.now());
        })
        .catch((e: unknown) => {
          if (seq !== reqSeq.current) return;
          if (quiet) setRefreshErr(true);
          else setErr(errText(e, "airdrops.cantConnect"));
        })
        .finally(() => {
          if (seq === reqSeq.current) {
            inflight.current = false;
            setLoading(false);
          }
          setNow(Date.now());
        });
    },
    [page, category, status, originFilter, verifyFilter],
  );

  useEffect(() => {
    void load();
  }, [load]);

  // live refresh: every 60 s while the tab is visible; paused while hidden; refreshes at once when shown again
  useEffect(
    () =>
      createAutoRefresh({
        intervalMs: AUTO_REFRESH_MS,
        run: () => void load(true),
        isHidden: () => document.hidden,
        onVisibility: (cb) => {
          document.addEventListener("visibilitychange", cb);
          return () => document.removeEventListener("visibilitychange", cb);
        },
      }),
    [load],
  );

  // keeps "更新于 x 分钟前" ticking between refreshes (cheap: only re-renders the label's parent every 15 s)
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), 15_000);
    return () => window.clearInterval(id);
  }, []);

  const pick = (fn: () => void) => {
    fn();
    setPage(1);
  };
  const go = (p: number) => {
    setPage(p);
    topRef.current?.scrollIntoView({ block: "start" });
  };
  const items = data?.items ?? [];
  const down = degradedFeeds(data?.feeds);
  const srvNow = serverClock(data?.serverNow, fetchedAt, now);
  const staleMin = data?.ingestEnabled === false ? null : staleMinutes(data?.lastIngestAt, srvNow, data?.ingestIntervalSec);

  return (
    <div className="pro-page airdrops-page" ref={topRef}>
      <header className="pro-head">
        <div>
          <h1>{t("airdrops.title")}</h1>
          <p>{t("airdrops.sub")}</p>
        </div>
        <span className="ad-upd muted" role="status" aria-live="off" title={t("airdrops.updTitle", { s: AUTO_REFRESH_MS / 1000 })}>
          {fetchedAt ? (
            <>
              <i className={`st-dot ${refreshErr ? "bad" : "ok"}`} aria-hidden="true" />
              {t("airdrops.updatedAt", { when: relWhen(t, fetchedAt, now) })}
              {data?.lastIngestAt ? <span className="ad-upd-src"> · {t("airdrops.ingestAt", { when: relWhen(t, data.lastIngestAt, srvNow) })}</span> : null}
            </>
          ) : (
            t("common.loadingDots")
          )}
        </span>
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
        <div className="ad-chips" aria-label={t("airdrops.filterVerify")}>
          {(
            [
              ["", t("airdrops.verify.all")],
              ["confirmed", t("airdrops.verify.confirmed")],
              ["unverified", t("airdrops.verify.unverified")],
            ] as ["" | AirdropVerification, string][]
          ).map(([v, label]) => (
            <button key={v || "all"} type="button" className={`ad-chip-btn ad-ver-btn${verifyFilter === v ? " is-active" : ""}`} onClick={() => pick(() => setVerifyFilter(v))}>
              {label}
            </button>
          ))}
        </div>
        <div className="ad-chips" aria-label={t("airdrops.filterOrigin")}>
          {(
            [
              ["", t("airdrops.originAll")],
              ["ingested", t("airdrops.origin.ingested")],
              ["curated", t("airdrops.origin.curated")],
            ] as ["" | "curated" | "ingested", string][]
          ).map(([o, label]) => (
            <button key={o || "all"} type="button" className={`ad-chip-btn${originFilter === o ? " is-active" : ""}`} onClick={() => pick(() => setOriginFilter(o))}>
              {label}
            </button>
          ))}
        </div>
      </div>

      {err ? <div className="pro-alert">{t("airdrops.loadFailed", { err })}</div> : null}
      {!err && refreshErr && data ? <div className="pro-alert is-warn">{t("airdrops.refreshFailed", { when: relWhen(t, fetchedAt, now) })}</div> : null}
      {down.length ? <div className="pro-alert is-warn">{t("airdrops.sourcesDown", { names: down.map((f) => f.name).join(", ") })}</div> : null}
      {staleMin !== null && !down.length ? <div className="pro-alert is-warn">{t("airdrops.stale", { min: staleMin })}</div> : null}

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
        {data?.verificationCounts ? (
          <p className="ad-counts">{t("airdrops.verifyCounts", { c: data.verificationCounts.confirmed, u: data.verificationCounts.unverified })}</p>
        ) : null}
        {(["official", "media"] as const).map((tier) => {
          const fs = (data?.feeds ?? []).filter((f) => (f.tier ?? "media") === tier);
          return fs.length > 0 ? (
            <div key={tier} className="ad-feeds">
              {t(tier === "official" ? "airdrops.feedsOfficial" : "airdrops.feedsMedia")}
              {fs.map((f) => (
                <span key={f.id} className="ad-feed-st">
                  <i className={`st-dot ${f.ok ? "ok" : f.lastAttemptAt ? "bad" : ""}`} aria-hidden="true" />
                  <a href={f.home} target="_blank" rel="noopener noreferrer nofollow">
                    {f.name}
                  </a>
                </span>
              ))}
            </div>
          ) : null;
        })}
        <p className="muted">{data?.note ?? t("airdrops.disclaimer")}</p>
      </footer>
    </div>
  );
}
