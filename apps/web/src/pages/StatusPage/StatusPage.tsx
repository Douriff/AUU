import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { marketProvider } from "@/providers/HttpWsProvider";
import type { PublicStatus } from "@/types/contracts";
import { useTranslation } from "react-i18next";
import i18n from "i18next";
import { errText } from "@/i18n/errors";
import { fmtDate, fmtFixed } from "@/i18n/format";

/** Login-free system status (coarse only: no positions, prices, versions or internal details). */

const POLL_MS = 60_000;

function pct(v: number | null | undefined, d = 2) {
  return v == null ? "—" : `${fmtFixed(v * 100, d)}%`;
}
function bj(ms: number | null | undefined) {
  if (!ms) return "—";
  return fmtDate(ms, { timeZone: "Asia/Shanghai", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false });
}
function dur(min: number) {
  return min >= 60 ? i18n.t("status.durHM", { h: Math.floor(min / 60), m: min % 60 }) : i18n.t("status.durM", { m: min });
}

function Row({ ok, label, value }: { ok: boolean | null; label: string; value: string }) {
  return (
    <li className="st-row">
      <span className={`st-dot ${ok == null ? "" : ok ? "ok" : "bad"}`} aria-hidden="true" />
      <span className="st-label">{label}</span>
      <span className="st-value num">{value}</span>
    </li>
  );
}

export function StatusPage() {
  const { t } = useTranslation();
  const [s, setS] = useState<PublicStatus | null>(null);
  const [err, setErr] = useState("");

  useEffect(() => {
    const load = () =>
      marketProvider
        .getPublicStatus()
        .then((x) => {
          setS(x);
          setErr("");
        })
        .catch((e: unknown) => setErr(errText(e, "news.cantConnect")));
    load();
    const timer = window.setInterval(() => !document.hidden && load(), POLL_MS);
    return () => window.clearInterval(timer);
  }, []);

  const u = s?.uptime30d;
  const md = s?.marketData;
  const st = s?.strategy;
  return (
    <div className="st-page">
      <header className="st-head">
        <h1>{t("auth.login.status")}</h1>
        <span className={`st-badge ${err ? "bad" : s?.healthy ? "ok" : s ? "warn" : ""}`}>
          {err ? t("status.apiDown") : !s ? t("status.checking") : s.healthy ? t("status.allOk") : t("status.partial")}
        </span>
      </header>
      {err ? <p className="pt-warn">{err}</p> : null}
      <ul className="st-list">
        <Row ok={err ? false : s ? true : null} label="API" value={err ? t("status.offline") : s ? t("status.online") : "—"} />
        <Row ok={md ? md.fresh : null} label={t("status.marketData")} value={md ? (md.fresh ? t("status.fresh") : t("status.staleSeries", { n: md.staleSeries })) : "—"} />
        <Row
          ok={md ? md.exchangesBlocked === 0 || md.exchangesBlocked < md.exchangesTotal : null}
          label={t("status.exchanges")}
          value={md ? (md.exchangesBlocked ? t("status.blocked", { n: md.exchangesBlocked, total: md.exchangesTotal }) : t("status.normal")) : "—"}
        />
        <Row
          ok={st ? !st.overdue : null}
          label={t("status.lastRebalance")}
          value={st?.lastRebalanceAt ? `${t("status.rebalanceAt", { time: bj(st.lastRebalanceAt), day: st.lastRebalanceDay })}${st.overdue ? ` · ${t("status.overdue")}` : ""}` : "—"}
        />
        <Row ok={s ? s.liveTrading === "locked" : null} label={t("status.live")} value={s ? (s.liveTrading === "locked" ? t("status.locked") : t("status.unlocked")) : "—"} />
      </ul>

      <section className="st-box">
        <div className="st-box-head">
          <b>{t("status.uptime30")}</b>
          <span className="muted">{u?.since ? t("status.since", { time: bj(u.since) }) : t("status.noRecord")}</span>
        </div>
        <div className="st-kpis num">
          <div><span className="muted">{t("status.apiUp")}</span><b>{pct(u?.upPct, 3)}</b></div>
          <div><span className="muted">{t("status.allPass")}</span><b>{pct(u?.healthyPct, 3)}</b></div>
        </div>
        <div className="st-bars" role="img" aria-label={t("status.daily")}>
          {(u?.days ?? []).map((d) => {
            const h = d.healthyPct ?? 0;
            const cls = d.measuredMin === 0 ? "" : h >= 0.999 ? "ok" : h >= 0.95 ? "warn" : "bad";
            return <span key={d.day} className={`st-bar ${cls}`} title={t("status.barTitle", { day: d.day, up: pct(d.upPct), ok: pct(d.healthyPct) })} />;
          })}
        </div>
        <p className="muted st-note">
          {t("status.method")}
        </p>
        <div className="st-box-head"><b>{t("status.recent")}</b></div>
        <ul className="st-outages num">
          {(u?.outages ?? []).map((o) => (
            <li key={`${o.kind}-${o.start}`}>
              <span className={o.kind === "down" ? "down" : "warn"}>{o.kind === "down" ? t("status.offline") : t("status.degraded")}</span>
              <span>{bj(o.start)}</span>
              <span className="muted">{dur(o.minutes)}</span>
            </li>
          ))}
          {u && u.outages.length === 0 ? <li className="muted st-empty">{t("status.noOutage")}</li> : null}
        </ul>
      </section>
      <p className="muted st-note">
        {t("status.foot")} · <Link to="/login">{t("auth.login.submit")}</Link>
      </p>
    </div>
  );
}
