import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { marketProvider } from "@/providers/HttpWsProvider";
import type { ReconSummary } from "@/types/mainstream";
import { fmtBjShort, fmtFixed, listJoin } from "@/i18n/format";
import { errText } from "@/i18n/errors";

/** Cross-source daily close reconciliation (read-only; never switches the strategy's data source). */

const POLL_MS = 300_000;
const pct = (v: number | null | undefined, d = 3) => (v == null || !Number.isFinite(v) ? "—" : `${fmtFixed(v, d)}%`);
const px = (v: number | null | undefined) => (v == null || !Number.isFinite(v) ? "—" : String(+v.toPrecision(6)));

export function ReconPanel() {
  const [d, setD] = useState<ReconSummary | null>(null);
  const [err, setErr] = useState("");
  const { t } = useTranslation();
  const st = (k: string) => t(`recon.st.${k}`, { defaultValue: k });
  useEffect(() => {
    let alive = true;
    const load = () =>
      marketProvider
        .getRecon()
        .then((x) => alive && (setD(x), setErr("")))
        .catch((e) => alive && setErr(errText(e, "common.loadFailed")));
    load();
    const id = window.setInterval(load, POLL_MS);
    return () => {
      alive = false;
      window.clearInterval(id);
    };
  }, []);
  const tone = !d?.lastRunAt ? "" : d.flagged ? "down" : d.status === "ok" ? "up" : "";
  return (
    <section className="console-card strat recon" aria-label={t("recon.aria")}>
      <header className="console-card-bar strat-head">
        <div className="console-title">{t("recon.title")}</div>
        <span className="console-badge">{t("recon.badge")}</span>
      </header>
      {err ? <p className="console-err">{err}</p> : null}
      <div className="strat-universe">
        <div>
          {t("recon.desc", {
            primary: d?.primary || "—",
            days: d?.days ?? 7,
            secondary: d?.secondary || t("recon.otherEx"),
            th: `${d ? d.thresholdPct : 0.5}%`,
            extra:
              d && Object.keys(d.thresholdOverrides).length
                ? t("recon.overrides", { list: listJoin(Object.entries(d.thresholdOverrides).map(([k, v]) => `${k} ${v}%`)) })
                : "",
          })}
        </div>
      </div>
      <div className="strat-kpis">
        <Stat label={t("recon.last")} value={d?.lastRunAt ? fmtBjShort(d.lastRunAt) : t("recon.never")} sub={d?.status ? st(d.status) : ""} />
        <Stat label={t("recon.checked")} value={d ? `${d.checked} / ${d.flagged}` : "—"} tone={tone} />
        <Stat label={t("recon.maxDev")} value={pct(d?.maxDevPct)} sub={d?.maxCoin || ""} />
        <Stat label={t("recon.srcErr")} value={d?.error ? t("common.yes") : t("common.no")} sub={d?.error ? d.error.slice(0, 60) : ""} />
      </div>
      <div className="strat-tables single">
        <div className="recon-scroll">
          <h4>{t("recon.latest", { day: d?.coins[0]?.latestDay || "—" })}</h4>
          <table className="num">
            <thead>
              <tr><th>{t("recon.col.coin")}</th><th>{d?.primary || t("recon.col.primary")}</th><th>{d?.secondary || t("recon.col.secondary")}</th><th>{t("recon.col.dev")}</th><th>{t("recon.col.max", { n: d?.days ?? 7 })}</th><th>{t("recon.col.status")}</th></tr>
            </thead>
            <tbody>
              {d?.coins.length ? (
                d.coins.map((c) => (
                  <tr key={c.coin}>
                    <td>{c.coin}</td>
                    <td>{px(c.c1)}</td>
                    <td>{px(c.c2)}</td>
                    <td>{pct(c.latestDev)}</td>
                    <td className={c.maxDev != null && c.maxDev > c.threshold ? "down" : ""}>{pct(c.maxDev)}</td>
                    <td className={c.flagged ? "down" : ""}>{c.flagged ? `${st(c.status)} ×${c.flagged}` : st("ok")}</td>
                  </tr>
                ))
              ) : (
                <tr><td colSpan={6} className="muted">{t("recon.empty")}</td></tr>
              )}
            </tbody>
          </table>
        </div>
      </div>
      {d?.flags.length ? (
        <div className="strat-universe">
          <b>{t("recon.flags")}</b>
          {d.flags.slice(0, 12).map((f) => (
            <div key={`${f.coin}-${f.day}`} className="down">
              {f.day} {f.coin} · {st(f.status)} · {px(f.c1)} / {px(f.c2)} · {pct(f.dev)} ({t("recon.th", { v: `${f.threshold}%` })})
            </div>
          ))}
        </div>
      ) : null}
    </section>
  );
}

function Stat({ label, value, sub, tone }: { label: string; value: string; sub?: string; tone?: string }) {
  return (
    <div className="console-stat">
      <span>{label}</span>
      <strong className={tone || ""}>{value}</strong>
      {sub ? <small className="muted">{sub}</small> : null}
    </div>
  );
}
