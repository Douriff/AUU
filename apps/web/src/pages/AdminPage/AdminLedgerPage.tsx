import { useState } from "react";
import { Link } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { marketProvider } from "@/providers/HttpWsProvider";
import { errText } from "@/i18n/errors";

/** Points ledger tools: reconcile + CSV export (user history lives under Users). */
export function AdminLedgerPage() {
  const { t } = useTranslation();
  const [verify, setVerify] = useState("");

  const runVerify = () => {
    marketProvider
      .adminPointsVerify()
      .then((v) => setVerify(v.ok ? t("points.admin.verifyOk", { n: v.entries, users: v.users }) : t("points.admin.verifyBad", { list: v.problems.join("; ") })))
      .catch((e: unknown) => setVerify(errText(e)));
  };

  return (
    <div className="shell-page settings-page pro-page points-admin">
      <header className="pro-head">
        <h1>{t("admin.nav.ledger")}</h1>
        <p>{t("admin.ledgerDesc")}</p>
      </header>
      <section className="settings-section">
        <div className="set-row">
          <div className="pts-tools">
            <a className="td-link" href={marketProvider.adminPointsExportUrl()} download>
              {t("points.admin.exportAll")}
            </a>
            <button type="button" className="td-link" onClick={runVerify}>
              {t("points.admin.verify")}
            </button>
            <Link className="td-link" to="/admin/users">
              {t("admin.nav.users")} ›
            </Link>
          </div>
        </div>
        {verify ? <p className="td-note">{verify}</p> : null}
        <p className="muted">{t("admin.ledgerHint")}</p>
      </section>
    </div>
  );
}
