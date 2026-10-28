import { Link } from "react-router-dom";
import { useTranslation } from "react-i18next";

const sections = [
  { to: "/admin/users", label: "users", desc: "usersDesc" },
  { to: "/admin/ledger", label: "ledger", desc: "ledgerDesc" },
  { to: "/admin/topups", label: "topups", desc: "topupsDesc" },
  { to: "/admin/trading", label: "trading", desc: "tradingDesc" },
  { to: "/admin/withdrawals", label: "withdrawals", desc: "withdrawalsDesc" },
  { to: "/admin/flags", label: "flags", desc: "flagsDesc" },
  { to: "/admin/exchange", label: "exchange", desc: "exchangeDesc" },
  { to: "/admin/status", label: "status", desc: "statusDesc" },
] as const;

/** Admin console home — section cards into dedicated panels. */
export function AdminHubPage() {
  const { t } = useTranslation();
  return (
    <div className="shell-page settings-page pro-page admin-hub">
      <header className="pro-head">
        <h1>{t("admin.title")}</h1>
        <p>{t("admin.sub")}</p>
      </header>
      <div className="admin-cards">
        {sections.map((s) => (
          <Link key={s.to} className="admin-card" to={s.to}>
            <h2>{t(`admin.nav.${s.label}`)}</h2>
            <p>{t(`admin.${s.desc}`)}</p>
            <span className="admin-card-go">{t("admin.open")} ›</span>
          </Link>
        ))}
      </div>
    </div>
  );
}
