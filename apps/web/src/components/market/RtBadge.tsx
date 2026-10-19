import { useTranslation } from "react-i18next";
import { useRtStatus } from "@/realtime/useRealtime";

/** Small "实时 / 轮询" marker. Renders nothing while the realtime feature is off (backend switch off). */
export function RtBadge() {
  const { t } = useTranslation();
  const st = useRtStatus();
  if (st.state === "off") return null;
  const live = st.state === "live";
  return (
    <span
      className={`rt-badge ${live ? "is-live" : "is-poll"}`}
      data-testid="rt-badge"
      data-state={st.state}
      data-reason={st.reason ?? ""}
      title={live ? t("rt.liveTitle", { src: (st.source ?? "").toUpperCase() }) : t("rt.pollingTitle")}
    >
      <i aria-hidden="true" />
      {live ? t("rt.live") : t("rt.polling")}
    </span>
  );
}
