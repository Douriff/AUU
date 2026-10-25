import { Link } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { SubPreviewToggle, useSubscription } from "@/components/subscribe/SubscribeGate";

/** $99/month plan entry. Display only — the button does not start checkout. */
export function SubscribePage() {
  const { t } = useTranslation();
  const { anon, isAdmin } = useSubscription();
  return (
    <div className="shell-page pro-page sub-page">
      <header className="pro-head">
        <h1>{t("subscribe.title")}</h1>
        <p>{t("subscribe.sub")}</p>
      </header>
      <section className="sub-plan">
        <p className="sub-price">
          <b>{t("subscribe.price")}</b> <span>{t("subscribe.per")}</span>
        </p>
        <h2>{t("subscribe.includes")}</h2>
        <ul>
          <li><span className="sub-star" aria-hidden="true">★</span> {t("subscribe.fMarket")}</li>
          <li><span className="sub-star" aria-hidden="true">★</span> {t("subscribe.fStars")}</li>
          <li><span className="sub-star" aria-hidden="true">★</span> {t("subscribe.fDesk")}</li>
        </ul>
        <button type="button" className="td-submit buy sub-cta" disabled>
          {t("subscribe.ctaPending")}
        </button>
        <p className="muted sub-note">{t("subscribe.noBilling")}</p>
        {anon ? (
          <p className="td-note">
            <Link to="/login">{t("subscribe.loginCta")}</Link>
          </p>
        ) : null}
      </section>
      {isAdmin ? (
        <section className="settings-section">
          <h2>{t("subscribe.preview")}</h2>
          <div className="set-row">
            <p className="muted">{t("subscribe.previewHint")}</p>
            <SubPreviewToggle />
          </div>
        </section>
      ) : null}
    </div>
  );
}
