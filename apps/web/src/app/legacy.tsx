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
      <h1>旧版 pump.fun 功能已停用</h1>
      <p>AUU 已转为主流币量化平台。pump.fun / Solana 模块已归档，服务端设置 AUU_LEGACY_PUMP=on 才会重新启用。</p>
      <p>
        <NavLink to="/">前往主流行情</NavLink>
      </p>
    </div>
  );
}
