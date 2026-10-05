import { Link } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { LangSwitch } from "@/components/ui/LangSwitch";
import { Brand, ModeBadge } from "@/components/ui/Brand";

/** Card header for login / register / forgot-password: brand, paper badge, page title. */
export function AuthHead({ title, sub }: { title: string; sub?: string }) {
  const { t } = useTranslation();
  return (
    <header className="auth-head">
      <div className="auth-head-brand">
        <Link to="/login" aria-label={t("nav.homeAria")}>
          <Brand />
        </Link>
        <span className="auth-head-tools">
          <LangSwitch compact />
          <ModeBadge />
        </span>
      </div>
      <h1>{title}</h1>
      {sub ? <p className="td-note auth-sub">{sub}</p> : null}
    </header>
  );
}
