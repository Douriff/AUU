import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { marketProvider } from "@/providers/HttpWsProvider";
import type { PointsFeatureConfig } from "@/types/contracts";
import { errText } from "@/i18n/errors";
import { fmtFixed } from "@/i18n/format";

/** Admin: points-trading P&L board (feature flags live under /admin/flags). */
export function AdminTradingPanel() {
  const { t } = useTranslation();
  const [cfg, setCfg] = useState<PointsFeatureConfig | null>(null);
  const [rows, setRows] = useState<{ userId: string; name: string; displayName: string; pointsBalance: number; equityPoints: number; pnlPoints: number; pnlUsdt: number; positions: number }[]>([]);
  const [err, setErr] = useState("");

  const load = useCallback(() => {
    marketProvider
      .adminPointsTrading()
      .then((r) => {
        setCfg(r.config);
        setRows(r.items);
      })
      .catch((e: unknown) => setErr(errText(e, "common.loadFailed")));
  }, []);
  useEffect(() => {
    load();
  }, [load]);

  return (
    <section className="settings-section">
      <h2>{t("points.admin.tradingTitle")}</h2>
      <p className="muted">{t("points.admin.tradingSub")}</p>
      {cfg ? (
        <p className="muted">
          {t("points.admin.tradingFlags", {
            on: cfg.enabled ? t("common.on") : t("common.off"),
            adminOnly: cfg.adminOnly ? t("common.on") : t("common.off"),
            rate: cfg.rate,
          })}
        </p>
      ) : null}
      {err ? <div className="pro-alert">{err}</div> : null}
      <div className="pro-table-wrap">
        <table className="pro-table">
          <thead>
            <tr>
              <th>{t("points.admin.user")}</th>
              <th>{t("points.balance")}</th>
              <th>{t("trade.equity")}</th>
              <th>{t("trade.totalPnl")}</th>
              <th>{t("trade.position", { sym: "" }).trim()}</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.userId}>
                <td>{r.displayName || r.name}</td>
                <td dir="ltr">{r.pointsBalance}</td>
                <td dir="ltr">{r.equityPoints}</td>
                <td dir="ltr" className={r.pnlPoints > 0 ? "up" : r.pnlPoints < 0 ? "down" : ""}>
                  {r.pnlPoints > 0 ? "+" : ""}
                  {r.pnlPoints} <span className="muted">({fmtFixed(r.pnlUsdt, 2)} USDT)</span>
                </td>
                <td dir="ltr">{r.positions}</td>
              </tr>
            ))}
            {rows.length === 0 ? (
              <tr>
                <td colSpan={5} className="muted">{t("common.none")}</td>
              </tr>
            ) : null}
          </tbody>
        </table>
      </div>
    </section>
  );
}
