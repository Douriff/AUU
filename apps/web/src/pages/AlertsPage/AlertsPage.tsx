import { useEffect, useState } from "react";
import { RiskTagBar } from "@/components/alerts/RiskTagBar";
import { marketProvider } from "@/providers/HttpWsProvider";

export function AlertsPage() {
  const [tags, setTags] = useState<string[]>(["LIVE_DISABLED"]);

  useEffect(() => {
    marketProvider
      .getLiveStatus()
      .then((st) => {
        const next = st.reasons?.length ? st.reasons : ["LIVE_DISABLED"];
        if (!next.includes("LIVE_DISABLED") && (st.liveDisabled || !st.liveEnabled)) {
          setTags(["LIVE_DISABLED", ...next]);
        } else {
          setTags(next);
        }
      })
      .catch(() => setTags(["LIVE_DISABLED"]));
  }, []);

  return (
    <div className="shell-page">
      <h1>告警 / Alerts</h1>
      <p className="muted">
        P0 路由壳。RiskOut.tags 已在行情顶栏 RiskTagBar 展示。Live path stays{" "}
        <code>LIVE_DISABLED</code> unless liveEnabled + confirm + mounted keypair + LiveLimits.
      </p>
      <RiskTagBar tags={tags} allow={false} />
    </div>
  );
}
