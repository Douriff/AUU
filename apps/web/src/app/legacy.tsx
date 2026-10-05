import type { ReactNode } from "react";
import { NavLink } from "react-router-dom";
import { useLegacyMode } from "@/hooks/useLegacyMode";
import { useTranslation } from "react-i18next";

/** `/`: pump board with AUU_LEGACY_PUMP=on, otherwise the mainstream market page. */
export function HomeIndex({ board, mainstream }: { board: ReactNode; mainstream: ReactNode }) {
  const legacy = useLegacyMode();
  if (legacy === null) return <div className="auth-screen" aria-busy="true" />;
  return <>{legacy ? board : mainstream}</>;
}

/** pump.fun pages render only when the API runs the legacy stack. */
export function LegacyOnly({ children }: { children: ReactNode }) {
  const legacy = useLegacyMode();
  const { t } = useTranslation();
  if (legacy === null) return null;
  if (legacy) return <>{children}</>;
  return (
    <div className="majors-page mj-dense">
      <h1>{t("legacy.title")}</h1>
      <p>{t("legacy.body")}</p>
      <p>
        <NavLink to="/">{t("legacy.go")}</NavLink>
      </p>
    </div>
  );
}
