import type { ReactNode } from "react";
import { NavLink } from "react-router-dom";
import { useLegacyMode } from "@/hooks/useLegacyMode";

/** `/`: pump board with AUU_LEGACY_PUMP=on, otherwise the mainstream market page. */
export function HomeIndex({ board, mainstream }: { board: ReactNode; mainstream: ReactNode }) {
  const legacy = useLegacyMode();
  if (legacy === null) return <div className="auth-screen" aria-busy="true" />;
  return <>{legacy ? board : mainstream}</>;
}

/** pump.fun pages render only when the API runs the legacy stack. */
export function LegacyOnly({ children }: { children: ReactNode }) {
  const legacy = useLegacyMode();
  if (legacy === null) return null;
  if (legacy) return <>{children}</>;
  return (
    <div className="majors-page mj-dense">
      <h1>该页面已停用</h1>
      <p>AUUTRADE 已转为主流币量化平台，旧版模块已归档。</p>
      <p>
        <NavLink to="/">前往主流行情</NavLink>
      </p>
    </div>
  );
}
