import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { marketProvider } from "@/providers/HttpWsProvider";
import type { StrategyReport } from "@/types/mainstream";
import { Empty, Sk, SkCards } from "@/components/ui/Skeleton";
import { H2ShadowCard } from "./H2ShadowCard";
import { useTranslation } from "react-i18next";
import { errText } from "@/i18n/errors";
import { fmtFixed, fmtMax } from "@/i18n/format";
import { goMessage, goVerdict } from "@/i18n/strategy";

/** P1-1/P1-2: strategy performance report (paper ledger). Login is enforced by AppShell + the API (401). */

const pct = (v: number | null | undefined, d = 2) =>
  v == null || !Number.isFinite(v) ? "—" : `${v > 0 ? "+" : ""}${fmtFixed(v * 100, d)}%`;
const fx = (v: number | null | undefined, d = 2) => (v == null || !Number.isFinite(v) ? "—" : fmtFixed(v, d));
const usd = (v: number | null | undefined) =>
  v == null || !Number.isFinite(v) ? "—" : `${v > 0 ? "+" : ""}${fmtFixed(v, 2)}`;
const MONTHS = ["1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "11", "12"];

function heat(v: number): string {
  const a = Math.min(1, Math.abs(v) / 0.08);
  return `color-mix(in srgb, var(${v >= 0 ? "--up" : "--down"}) ${Math.round(14 + 56 * a)}%, transparent)`;
}
const tn = (v: number | null | undefined) => (v == null || !Number.isFinite(v) || v === 0 ? "" : v > 0 ? "up" : "down");

export function PerformancePage() {
  const { t } = useTranslation();
  const [rep, setRep] = useState<StrategyReport | null>(null);
  const [err, setErr] = useState("");

  useEffect(() => {
    let alive = true;
    marketProvider
      .getStrategyReport()
      .then((d) => alive && (setRep(d), setErr("")))
      .catch((e) => alive && setErr(errText(e, "common.loadFailed")));
    return () => {
      alive = false;
    };
  }, []);

  const years = useMemo(() => {
    const by: Record<string, Record<string, { ret: number; days: number }>> = {};
    for (const m of rep?.monthly ?? []) {
      const [y, mo] = m.month.split("-");
      (by[y] ||= {})[String(Number(mo))] = { ret: m.ret, days: m.days };
    }
    return Object.entries(by).sort(([a], [b]) => a.localeCompare(b));
  }, [rep]);

  const m = rep?.metrics;
  const g = rep?.goNoGo;
  return (
    <div className="shell-page perf-page pro-page">
      <header className="pro-head">
        <h1>{t("perf.title")}</h1>
        <p>{rep ? t("perf.sub", { strategy: rep.strategy, asOf: rep.asOf ?? "—", start: fmtMax(rep.startNav, 2) }) : <Sk w={260} h={11} />}</p>
        <Link to="/console" className="pro-head-link">{t("perf.consoleLink")} ›</Link>
      </header>
      {err ? <div className="pro-alert">{err}</div> : null}
      {!rep && !err ? (
        <>
          <Sk h={64} r={6} className="sk-block" />
          <SkCards n={12} h={58} />
          <Sk h={180} r={6} className="sk-block" />
        </>
      ) : null}
      {g ? (
        <section className={`perf-verdict lamp-${g.lamp}`}>
          <b>Go/No-Go: {goVerdict(g)}</b>
          <span>{goMessage(g)}</span>
          <span className="muted">
            {t("perf.ci")} {rep?.ci ? `${pct(rep.ci.lo)} ~ ${pct(rep.ci.hi)}` : t("perf.ciShort", { n: m?.days ?? 0 })} · {t("perf.criteria", { min: g.minDays })}
          </span>
        </section>
      ) : null}

      {m && m.days ? (
        <section className="perf-kpis">
          {(
            [
              ["total", t("perf.k.total"), pct(m.totalReturn), tn(m.totalReturn)],
              ["cagr", t("perf.k.cagr"), m.shortSample ? "—" : pct(m.cagr), m.shortSample ? "" : tn(m.cagr)],
              ["sortino", "Sortino", m.shortSample ? "—" : fx(m.sortino), ""],
              ["calmar", "Calmar", m.shortSample ? "—" : fx(m.calmar), ""],
              ["sharpe", "Sharpe", m.shortSample ? "—" : fx(m.sharpe), ""],
              ["mdd", t("perf.k.mdd"), `${pct(m.maxDrawdown)}${m.maxDrawdownDay ? ` · ${m.maxDrawdownDay}` : ""}`, tn(m.maxDrawdown)],
              ["longest", t("perf.k.longest"), t("perf.nDays", { n: m.longestDrawdownDays ?? 0 }), ""],
              ["cur", t("perf.k.current"), `${pct(m.currentDrawdown)} · ${t("perf.nDays", { n: m.currentDrawdownDays ?? 0 })}`, tn(m.currentDrawdown)],
              ["win", t("perf.k.winRate"), pct(m.winRate, 1), ""],
              ["wl", t("perf.k.winLoss"), fx(m.winLossRatio), ""],
              ["bw", t("perf.k.bestWorst"), `${pct(m.bestDay)} / ${pct(m.worstDay)}`, ""],
              ["days", t("perf.k.days"), String(m.days), ""],
            ] as [string, string, string, string][]
          ).map(([id, k, v, cls]) => (
            <div key={id} className="perf-kpi">
              <span className="muted">{k}</span>
              <b className={cls}>{v}</b>
            </div>
          ))}
          {m.shortSample ? <p className="muted perf-warn">{t("perf.shortSample")}</p> : null}
        </section>
      ) : rep ? (
        <Empty icon="chart" title={t("perf.empty")} hint={t("perf.emptyHint")} />
      ) : null}

      <H2ShadowCard />

      {years.length ? (
        <section className="perf-card">
          <h2>{t("perf.monthly")}</h2>
          <div className="perf-scroll">
            <table className="perf-heat num">
              <thead>
                <tr>
                  <th>{t("perf.year")}</th>
                  {MONTHS.map((x) => (
                    <th key={x}>{t("perf.month", { m: x })}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {years.map(([y, row]) => (
                  <tr key={y}>
                    <th>{y}</th>
                    {MONTHS.map((x) => {
                      const c = row[x];
                      return (
                        <td key={x} style={c ? { background: heat(c.ret) } : undefined} title={c ? t("perf.nDays", { n: c.days }) : ""}>
                          {c ? pct(c.ret, 1) : ""}
                        </td>
                      );
                    })}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      ) : null}

      {rep && rep.drawdown.length ? <DrawdownChart rep={rep} /> : null}

      {rep ? (
        <section className="perf-card">
          <h2>{t("perf.attr")}</h2>
          <div className="perf-scroll">
            <table className="perf-attr num">
              <thead>
                <tr>
                  <th>{t("perf.a.coin")}</th>
                  <th>{t("perf.a.price")}</th>
                  <th>{t("perf.a.funding")}</th>
                  <th>{t("perf.a.cost")}</th>
                  <th>{t("perf.a.total")}</th>
                </tr>
              </thead>
              <tbody>
                {rep.attribution.coins.map((r) => (
                  <tr key={r.coin}>
                    <td>{r.coin}</td>
                    <td className={r.price >= 0 ? "up" : "down"}>{usd(r.price)}</td>
                    <td className={r.funding >= 0 ? "up" : "down"}>{rep.attribution.fundingByCoin ? usd(r.funding) : "—"}</td>
                    <td className="down">{usd(r.cost)}</td>
                    <td className={r.total >= 0 ? "up" : "down"}>
                      <b>{usd(r.total)}</b>
                    </td>
                  </tr>
                ))}
              </tbody>
              <tfoot>
                <tr>
                  <td colSpan={4}>{t("perf.a.ledger")}</td>
                  <td>{usd(rep.attribution.totalUsd)}</td>
                </tr>
                <tr>
                  <td colSpan={4}>{t("perf.a.residual")}</td>
                  <td>{usd(rep.attribution.residualUsd)}</td>
                </tr>
              </tfoot>
            </table>
          </div>
          <p className="muted perf-note">
            {t("perf.a.note")}
            {rep.attribution.segmentsFromAdjustments ? ` ${t("perf.a.segments", { n: rep.attribution.segmentsFromAdjustments })}` : ""}
            {rep.attribution.fundingByCoin ? "" : ` ${t("perf.a.noFundingByCoin")}`}
          </p>
        </section>
      ) : null}
      {rep ? <p className="muted perf-note">{t("perf.note", { min: rep.goNoGo?.minDays ?? 250 })}</p> : null}
    </div>
  );
}

function DrawdownChart({ rep }: { rep: StrategyReport }) {
  const pts = rep.drawdown;
  const W = 600, H = 160, HN = 120;
  const x = (i: number) => (pts.length > 1 ? (i / (pts.length - 1)) * W : W / 2);
  const minDd = Math.min(-0.01, ...pts.map((p) => p.dd));
  const yd = (v: number) => (v / minDd) * H;
  const navs = pts.map((p) => p.nav);
  const lo = Math.min(rep.startNav, ...navs), hi = Math.max(rep.startNav, ...navs);
  const yn = (v: number) => HN - ((v - lo) / (hi - lo || 1)) * HN;
  const dd = `M0,0${pts.map((p, i) => `L${x(i).toFixed(1)},${yd(p.dd).toFixed(1)}`).join("")}L${W},0Z`;
  const nav = pts.map((p, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${yn(p.nav).toFixed(1)}`).join("");
  const m = rep.metrics;
  const { t } = useTranslation();
  return (
    <section className="perf-card">
      <h2>{t("perf.navDd")}</h2>
      <svg viewBox={`0 0 ${W} ${HN}`} className="perf-svg" preserveAspectRatio="none" role="img" aria-label={t("perf.nav")}>
        <line x1={0} x2={W} y1={yn(rep.startNav)} y2={yn(rep.startNav)} stroke="currentColor" opacity={0.25} strokeDasharray="4 4" />
        <path d={nav} fill="none" stroke="#f0a531" strokeWidth={2} vectorEffect="non-scaling-stroke" />
      </svg>
      <svg viewBox={`0 0 ${W} ${H}`} className="perf-svg perf-dd" preserveAspectRatio="none" role="img" aria-label={t("perf.dd")}>
        <path d={dd} className="dd-area" strokeWidth={1} vectorEffect="non-scaling-stroke" />
      </svg>
      <p className="muted perf-note">
        {pts[0].day} ~ {pts[pts.length - 1].day} · {t("perf.k.mdd")} {pct(m.maxDrawdown)} · {t("perf.k.longest")} {t("perf.nDays", { n: m.longestDrawdownDays ?? 0 })}
        {m.longestDrawdown ? ` (${m.longestDrawdown.from} ~ ${m.longestDrawdown.to})` : ""} · {t("perf.ddAxis", { v: pct(minDd) })}
      </p>
    </section>
  );
}
