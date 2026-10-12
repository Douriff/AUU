import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";

/** Page chrome for admin section routes. Pass titleKey for a top heading; omit when the panel already has one. */
export function AdminSectionPage({ titleKey, subKey, children }: { titleKey?: string; subKey?: string; children: ReactNode }) {
  const { t } = useTranslation();
  return (
    <div className="shell-page settings-page pro-page points-admin">
      {titleKey ? (
        <header className="pro-head">
          <h1>{t(titleKey)}</h1>
          {subKey ? <p>{t(subKey)}</p> : null}
        </header>
      ) : null}
      {children}
    </div>
  );
}
