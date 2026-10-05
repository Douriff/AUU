import { useEffect, useState } from "react";
import { marketProvider } from "@/providers/HttpWsProvider";
import type { ShadowH2Summary } from "@/types/mainstream";
import { Sk } from "@/components/ui/Skeleton";
import { useTranslation } from "react-i18next";
import { errText } from "@/i18n/errors";
import { fmtDate, fmtFixed } from "@/i18n/format";

/** H2 forward shadow (pre-registered, frozen params, no capital). Read-only; login enforced by AppShell + API 401. */

const DAY_MS = 86_400_000;
const pct = (v: number | null | undefined, d = 2) =>
  v == null || !Number.isFinite(v) ? "—" : `${v > 0 ? "+" : ""}${fmtFixed(v * 100, d)}%`;
const usd = (v: number | null | undefined) =>
  v == null || !Number.isFinite(v) ? "—" : `${v > 0 ? "+" : ""}${fmtFixed(v, 2)}`;
const tn = (v: number | null | undefined) => (v == null || !Number.isFinite(v) || v === 0 ? "" : v > 0 ? "up" : "down");

/** "2026-10-04" -> "10/4" (zh) / "10/4" (en) / "4.10." (de) … month + day in the UI language */
export function shortDay(day: string | null | undefined): string {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(day ?? "");
  if (!m) return day ?? "—";
  return fmtDate(Date.UTC(Number(m[1]), Number(m[2]) - 1, Number(m[3]), 12), { timeZone: "UTC", month: "numeric", day: "numeric" });
}

/** inception + n days, as YYYY-MM-DD (UTC days, same as the ledger). */
export function addDays(day: string | null | undefined, n: number): string {
  const t = Date.parse(`${day}T00:00:00Z`);
  return Number.isFinite(t) ? new Date(t + n * DAY_MS).toISOString().slice(0, 10) : "—";
}

export function H2ShadowCard() {
  const { t } = useTranslation();
  const [h2, setH2] = useState<ShadowH2Summary | null>(null);
  const [err, setErr] = useState("");

  useEffect(() => {
    let alive = true;
    marketProvider
      .getShadowH2()
      .then((d) => alive && (setH2(d), setErr("")))
      .catch((e) => alive && setErr(errText(e, "common.loadFailed")));
    return () => {
      alive = false;
    };
  }, []);

  const days = h2?.gate?.forward_days ?? 180;
  const n = Math.min(days, Math.max(0, h2?.progress ?? 0));
  const inc = h2?.inceptionDay ?? "2026-10-04";
  const end = addDays(inc, days);
  const last = h2?.last ?? null;
  const cum = h2?.cumulative ?? null;
  const ddLimit = h2?.abort?.max_drawdown ?? -0.05;

  return (
    <section className="perf-card h2-card" data-testid="h2-shadow">
      <div className="h2-head">
        <h2>{t("h2.title")}</h2>
        <span className="tag h2-tag">{t("h2.tag")}</span>
        {h2?.paramsSha256 ? (
          <span className={`h2-frozen ${h2.paramsFrozen === false || h2.refused ? "down" : "dim"}`} title={h2.paramsSha256}>
            {h2.paramsFrozen === false || h2.refused ? t("h2.hashMismatch") : t("h2.frozen")} · {h2.paramsSha256.slice(0, 12)}
          </span>
        ) : null}
      </div>

      {err ? <div className="pro-alert">{t("h2.loadFailed", { err })}</div> : null}
      {!h2 && !err ? <Sk h={88} r={6} className="sk-block" /> : null}

      {h2?.refused ? <div className="pro-alert">{t("h2.refused", { why: h2.refused })}</div> : null}
      {h2?.aborted ? <div className="pro-alert">{t("h2.aborted", { why: h2.aborted })}</div> : null}
      {h2?.paused ? <div className="h2-warn">{t("h2.paused", { n: h2.lagDays })}</div> : null}

      {h2 && !last ? (
        <div className="h2-empty">
          <b>{t("h2.registered", { day: shortDay(inc) })}</b>
          <span className="muted">{t("h2.firstRow")}</span>
          {h2.waiting ? <span className="muted">{t("h2.waiting", { s: h2.waiting })}</span> : null}
        </div>
      ) : null}

      {last && cum ? (
        <>
          <div className="perf-kpis h2-kpis">
            {(
              [
                [t("h2.k.day", { day: shortDay(last.dayStr) }), pct(last.ret, 3), tn(last.ret)],
                [t("perf.k.total"), pct(cum.ret, 3), tn(cum.ret)],
                [t("h2.k.excess"), pct(cum.excess, 3), tn(cum.excess)],
                [t("h2.k.tbill"), pct(cum.tbill, 3), ""],
                [t("h2.k.nav"), fmtFixed(last.nav, 2), ""],
                [t("h2.k.mdd", { v: pct(ddLimit, 0) }), pct(cum.maxDrawdown), tn(cum.maxDrawdown)],
              ] as [string, string, string][]
            ).map(([k, v, c]) => (
              <div key={k} className="perf-kpi">
                <span className="muted">{k}</span>
                <b className={c}>{v}</b>
              </div>
            ))}
          </div>
          <div className="perf-scroll">
            <table className="perf-attr num h2-legs">
              <thead>
                <tr>
                  <th>{t("h2.legs")}</th>
                  <th>{t("h2.today")}</th>
                  <th>{t("h2.cum")}</th>
                </tr>
              </thead>
              <tbody>
                {(
                  [
                    [t("h2.trend", { w: fmtFixed(last.w_trend * 100, 1) }), last.pnl_trend, cum.pnlTrend],
                    [t("h2.carry", { w: fmtFixed(last.w_carry * 100, 1) }), last.pnl_carry, cum.pnlCarry],
                    [t("h2.idle"), last.pnl_idle, cum.pnlIdle],
                    [t("h2.rebalCost"), 0 - last.cost_sleeve, 0 - cum.costSleeve],
                  ] as [string, number, number][]
                ).map(([k, d, c]) => (
                  <tr key={k}>
                    <td>{k}</td>
                    <td className={tn(d)}>{usd(d)}</td>
                    <td className={tn(c)}>{usd(c)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      ) : null}

      {h2 ? (
        <div className="h2-progress">
          <div className="h2-progress-top">
            <span>{t("h2.progress")}</span>
            <b className="num">
              {t("h2.progressDays", { n, days })}
            </b>
            <span className="muted num">
              {shortDay(inc)} → {end}
            </span>
          </div>
          <div className="h2-bar" role="progressbar" aria-valuemin={0} aria-valuemax={days} aria-valuenow={n}>
            <i style={{ width: `${(n / days) * 100}%` }} />
          </div>
        </div>
      ) : null}

      {h2 ? (
        <p className="muted perf-note h2-gate">
          {t("h2.gate", { days, end })} {t("h2.abort", { dd: pct(Math.abs(ddLimit), 0).replace("+", ""), days })}
        </p>
      ) : null}
    </section>
  );
}
