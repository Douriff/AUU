import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { marketProvider } from "@/providers/HttpWsProvider";
import { Empty, SkCards } from "@/components/ui/Skeleton";
import type { AirdropItem } from "@/types/mainstream";
import { useTranslation } from "react-i18next";
import { errText } from "@/i18n/errors";
import { fmtDate } from "@/i18n/format";

function bj(ms: number | null | undefined) {
  if (!ms) return "—";
  return fmtDate(ms, {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
}

export function AirdropDetailPage() {
  const { t } = useTranslation();
  const { id = "" } = useParams<{ id: string }>();
  const [item, setItem] = useState<AirdropItem | null>(null);
  const [note, setNote] = useState("");
  const [err, setErr] = useState("");
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    if (!id) {
      setErr("missing");
      setLoading(false);
      return;
    }
    setLoading(true);
    marketProvider
      .getAirdrop(id)
      .then((d) => {
        setItem(d.item);
        setNote(d.note);
        setErr("");
      })
      .catch((e: unknown) => {
        setItem(null);
        setErr(errText(e, "airdrops.cantConnect"));
      })
      .finally(() => setLoading(false));
  }, [id]);

  const original = item?.externalUrl || item?.sourceUrl || null;

  return (
    <div className="pro-page airdrops-page ad-detail">
      <nav className="ad-crumb">
        <Link to="/airdrops">‹ {t("airdrops.back")}</Link>
      </nav>

      {loading ? (
        <SkCards n={2} h={140} />
      ) : err || !item ? (
        <Empty icon="search" title={t("airdrops.notFound")} hint={t("airdrops.notFoundHint")} />
      ) : (
        <article className="ad-detail-card">
          <div className="ad-meta">
            <span className="ad-badge ad-cat">{t(`airdrops.cat.${item.category}`)}</span>
            <span className={`ad-badge ad-st is-${item.status}`}>{t(`airdrops.status.${item.status}`)}</span>
          </div>
          <h1 className="ad-detail-title">{item.title}</h1>
          <p className="ad-project">{item.projectName}</p>
          <dl className="ad-kv">
            <div>
              <dt>{t("airdrops.source")}</dt>
              <dd>
                {item.sourceUrl ? (
                  <a href={item.sourceUrl} target="_blank" rel="noopener noreferrer nofollow">
                    {item.sourceName}
                  </a>
                ) : (
                  item.sourceName
                )}
              </dd>
            </div>
            <div>
              <dt>{t("airdrops.published")}</dt>
              <dd>
                <time dateTime={new Date(item.publishedAt).toISOString()}>{bj(item.publishedAt)}</time>
              </dd>
            </div>
            <div>
              <dt>{t("airdrops.updated")}</dt>
              <dd>
                <time dateTime={new Date(item.updatedAt).toISOString()}>{bj(item.updatedAt)}</time>
              </dd>
            </div>
            {item.chains.length ? (
              <div>
                <dt>{t("airdrops.chains")}</dt>
                <dd className="ad-chains">
                  {item.chains.map((c) => (
                    <span key={c} className="ad-chip">
                      {c}
                    </span>
                  ))}
                </dd>
              </div>
            ) : null}
            {item.tags.length ? (
              <div>
                <dt>{t("airdrops.tags")}</dt>
                <dd className="ad-chains">
                  {item.tags.map((tg) => (
                    <span key={tg} className="ad-chip">
                      {tg}
                    </span>
                  ))}
                </dd>
              </div>
            ) : null}
          </dl>
          {item.summary ? (
            <section className="ad-body">
              <h2>{t("airdrops.summary")}</h2>
              <p>{item.summary}</p>
            </section>
          ) : null}
          {original ? (
            <p className="ad-cta">
              <a className="btn-primary ad-btn" href={original} target="_blank" rel="noopener noreferrer nofollow">
                {t("airdrops.viewOriginal")}
              </a>
            </p>
          ) : null}
          <p className="muted ad-disclaimer">{note || t("airdrops.disclaimer")}</p>
        </article>
      )}
    </div>
  );
}
