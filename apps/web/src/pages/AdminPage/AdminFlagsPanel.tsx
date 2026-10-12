import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { marketProvider } from "@/providers/HttpWsProvider";
import { errText } from "@/i18n/errors";

/** Read-only feature-flag summary for points trading / withdraw / TRC20. */
export function AdminFlagsPanel() {
  const { t } = useTranslation();
  const [flags, setFlags] = useState("");
  const [err, setErr] = useState("");

  const load = useCallback(() => {
    marketProvider
      .adminPointsFlags()
      .then((f) => {
        setFlags(
          t("points.admin.flagsLine", {
            trading: f.pointsTrading.enabled ? "on" : "off",
            tradingAdmin: f.pointsTrading.adminOnly ? "admin" : "all",
            withdraw: f.withdraw.enabled ? "on" : "off",
            withdrawAdmin: f.withdraw.adminOnly ? "admin" : "all",
            trc20: f.trc20Topup.enabled ? "on" : "off",
          }),
        );
      })
      .catch((e: unknown) => setErr(errText(e, "common.loadFailed")));
  }, [t]);

  useEffect(() => {
    load();
  }, [load]);

  return (
    <div className="shell-page settings-page pro-page">
      <header className="pro-head">
        <h1>{t("admin.nav.flags")}</h1>
        <p>{t("admin.flagsDesc")}</p>
      </header>
      <section className="settings-section">
        {err ? <div className="pro-alert">{err}</div> : null}
        {flags ? <p className="td-note">{flags}</p> : <p className="muted">{t("common.loading")}</p>}
        <p className="muted">{t("admin.flagsHint")}</p>
      </section>
    </div>
  );
}
