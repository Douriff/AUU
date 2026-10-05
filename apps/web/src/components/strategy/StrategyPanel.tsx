import { useEffect, useRef, useState } from "react";
import { ColorType, createChart, type IChartApi, type ISeriesApi, type UTCTimestamp } from "lightweight-charts";
import { Link } from "react-router-dom";
import { marketProvider } from "@/providers/HttpWsProvider";
import { SkCards } from "@/components/ui/Skeleton";
import { CHART_CHROME, chartColors, onColorPref } from "@/theme/colorPref";
import type { ExecShadowSummary, ExpectedBand, RunVersion, StrategyRisk, StrategySummary } from "@/types/mainstream";
import { useTranslation } from "react-i18next";
import i18n from "i18next";
import { errText } from "@/i18n/errors";
import { fmtBjShort, fmtDate, fmtFixed, intlLocale } from "@/i18n/format";
import { goMessage, goVerdict } from "@/i18n/strategy";

/** M3: daily paper runner (trend_tsmom_v1) — equity vs BTC buy&hold vs T-bill, daily returns, positions. */

const POLL_MS = 60_000;
const COL = { strat: "#f0a531", btc: "#7d8fb3", tbill: "#5f6672" };

const pct = (v: number | null | undefined, d = 2) =>
  v == null || !Number.isFinite(v) ? "—" : `${v > 0 ? "+" : ""}${fmtFixed(v * 100, d)}%`;
const num = (v: number | null | undefined, d = 2) => (v == null || !Number.isFinite(v) ? "—" : fmtFixed(v, d));
const tone = (v: number | null | undefined) => (v == null || !v ? "flat" : v > 0 ? "up" : "down");
const SH = { format: (d: Date) => fmtBjShort(d.getTime()) };
const tx = (group: string, k: string, fallback?: string) => (i18n.exists(`strat.${group}.${k}`) ? i18n.t(`strat.${group}.${k}`) : fallback ?? k);

export function StrategyPanel() {
  const { t, i18n: inst } = useTranslation();
  const [data, setData] = useState<StrategySummary | null>(null);
  const [err, setErr] = useState("");
  const host = useRef<HTMLDivElement>(null);
  const chart = useRef<IChartApi | null>(null);
  const series = useRef<{ s?: ISeriesApi<"Line">; b?: ISeriesApi<"Line">; t?: ISeriesApi<"Line">; r?: ISeriesApi<"Histogram"> }>({});

  useEffect(() => {
    let alive = true;
    const load = () =>
      marketProvider
        .getStrategySummary()
        .then((d) => alive && (setData(d), setErr("")))
        .catch((e) => alive && setErr(errText(e, "common.loadFailed")));
    load();
    const id = window.setInterval(load, POLL_MS);
    return () => {
      alive = false;
      window.clearInterval(id);
    };
  }, []);

  useEffect(() => {
    if (!host.current) return;
    const c = createChart(host.current, {
      autoSize: true,
      layout: { background: { type: ColorType.Solid, color: "transparent" }, textColor: CHART_CHROME.text, fontSize: 11, fontFamily: CHART_CHROME.font },
      grid: { vertLines: { color: CHART_CHROME.grid }, horzLines: { color: CHART_CHROME.grid } },
      rightPriceScale: { borderColor: CHART_CHROME.border, scaleMargins: { top: 0.08, bottom: 0.3 } },
      timeScale: { borderColor: CHART_CHROME.border, rightOffset: 2 },
      handleScroll: { vertTouchDrag: false },
      localization: { locale: intlLocale() },
    });
    const fmt = { type: "custom" as const, formatter: (v: number) => `${v > 0 ? "+" : ""}${fmtFixed(v, 2)}%` };
    series.current = {
      s: c.addLineSeries({ color: COL.strat, lineWidth: 2, priceFormat: fmt, title: i18n.t("strat.series.strategy") }),
      b: c.addLineSeries({ color: COL.btc, lineWidth: 1, priceFormat: fmt, title: "BTC" }),
      t: c.addLineSeries({ color: COL.tbill, lineWidth: 1, lineStyle: 2, priceFormat: fmt, title: i18n.t("strat.series.tbill") }),
      r: c.addHistogramSeries({ priceScaleId: "ret", priceFormat: fmt, lastValueVisible: false, priceLineVisible: false }),
    };
    c.priceScale("ret").applyOptions({ scaleMargins: { top: 0.75, bottom: 0 } });
    chart.current = c;
    return () => {
      c.remove();
      chart.current = null;
    };
  }, []);

  // Series titles / axis locale follow the interface language.
  useEffect(() => {
    chart.current?.applyOptions({ localization: { locale: intlLocale() } });
    series.current.s?.applyOptions({ title: i18n.t("strat.series.strategy") });
    series.current.t?.applyOptions({ title: i18n.t("strat.series.tbill") });
  }, [inst.language]);

  // Daily-return bars follow the 红涨绿跌 toggle (canvas colours are set from JS).
  const [colorRev, setColorRev] = useState(0);
  useEffect(() => onColorPref(() => setColorRev((n) => n + 1)), []);

  useEffect(() => {
    if (!data || !chart.current) return;
    const t = (ts: number) => (ts / 1000) as UTCTimestamp;
    const { s, b, t: tb, r } = series.current;
    const COLR = chartColors();
    s?.setData(data.curve.map((p) => ({ time: t(p.ts), value: (p.strategy - 1) * 100 })));
    b?.setData(data.curve.filter((p) => p.btc != null).map((p) => ({ time: t(p.ts), value: ((p.btc as number) - 1) * 100 })));
    tb?.setData(data.curve.map((p) => ({ time: t(p.ts), value: (p.tbill - 1) * 100 })));
    r?.setData(data.curve.map((p) => ({ time: t(p.ts), value: p.ret * 100, color: p.ret >= 0 ? COLR.up : COLR.down })));
    const ts = chart.current.timeScale();
    if (data.curve.length >= 30) ts.fitContent();
    else {
      ts.applyOptions({ barSpacing: 24 });
      ts.scrollToRealTime();
    }
  }, [data]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (!colorRev || !data) return;
    const COLR = chartColors();
    series.current.r?.setData(data.curve.map((p) => ({ time: (p.ts / 1000) as UTCTimestamp, value: p.ret * 100, color: p.ret >= 0 ? COLR.up : COLR.down })));
  }, [colorRev]); // eslint-disable-line react-hooks/exhaustive-deps

  const g = data?.goNoGo;
  const st = data?.status;
  const last = data?.curve[data.curve.length - 1];
  return (
    <section className="console-card strat" aria-label={t("ms.stratCard")}>
      <header className="console-card-bar strat-head">
        <div className="console-title">{t("strat.title", { name: data?.strategy.name || "trend_tsmom_v1" })}</div>
        <span className="strat-sub">{t("strat.daily")}</span>
        <Link to="/performance" className="console-badge strat-report-link">{t("strat.report")} →</Link>
        {g ? (
          <span className={`console-go lamp-${g.lamp}`} title={goMessage(g)}>
            {goVerdict(g)}
          </span>
        ) : null}
      </header>
      {err ? <p className="console-err">{err}</p> : null}
      {!data && !err ? <SkCards n={8} h={62} /> : null}
      <div className="strat-kpis" hidden={!data}>
        <Kpi label={t("strat.k.nav")} value={num(data?.nav)} />
        <Kpi label={t("strat.k.cum")} value={pct(data?.totals.strategy)} tone={tone(data?.totals.strategy)} />
        <Kpi label={t("strat.k.btc")} value={pct(data?.totals.btc)} tone={tone(data?.totals.btc ?? 0)} />
        <Kpi label={t("strat.k.tbill", { r: "3.99%" })} value={pct(data?.totals.tbill)} />
        <Kpi label={t("strat.k.lastRet")} value={pct(last?.ret, 3)} tone={tone(last?.ret)} />
        <Kpi label={t("ms.grossExposure")} value={last ? `${fmtFixed(last.gross * 100, 1)}%` : "—"} />
        <Kpi
          label={t("status.lastRebalance")}
          value={st?.lastDay ? t("strat.k.close", { day: st.lastDay }) : t("strat.k.none")}
          sub={st?.lastRunAt ? t("strat.k.ranAt", { time: SH.format(new Date(st.lastRunAt)) }) : st?.waiting || ""}
        />
        <Kpi
          label={t("strat.k.state")}
          value={st ? (st.stalled ? t("strat.k.stalled", { h: fmtFixed(st.hoursSinceRebalance, 1) }) : t("strat.k.ok", { h: fmtFixed(st.hoursSinceRebalance, 1) })) : "—"}
          tone={st?.stalled ? "bad" : "ok"}
          sub={st ? t("strat.k.running", { days: st.days, h: st.stallHours }) : ""}
        />
      </div>
      {g ? <p className={`strat-go lamp-${g.lamp}`}>{goMessage(g)} {t("strat.goStd", { min: g.minDays })}</p> : null}
      {data ? <UniverseNote data={data} /> : null}
      {data?.risk?.enabled ? <RiskBox risk={data.risk} /> : null}
      <BandBox b={data?.expectedBand ?? null} />
      <ExecShadowBox s={data?.execShadow ?? null} />
      {data?.version ? <VersionLine v={data.version} /> : null}
      <div className="strat-legend">
        <i style={{ background: COL.strat }} />{t("strat.series.strategy")} <i style={{ background: COL.btc }} />{t("strat.k.btc")} <i style={{ background: COL.tbill }} />{t("strat.series.tbill")} <i className="bar" />{t("strat.series.daily")}
      </div>
      <div className="strat-chart" ref={host} />
      <div className="strat-tables">
        <div>
          <h4>{t("strat.positions", { n: data?.positions.length ?? 0 })}</h4>
          <table className="num">
            <thead>
              <tr><th>{t("perf.a.coin")}</th><th>{t("strat.col.weight")}</th><th>{t("trade.value")}</th><th>{t("ob.qty")}</th><th>{t("strat.col.close")}</th></tr>
            </thead>
            <tbody>
              {data?.positions.length ? (
                data.positions.map((p) => (
                  <tr key={p.coin}>
                    <td>{p.coin}</td>
                    <td>{fmtFixed(p.weight * 100, 2)}%</td>
                    <td>{num(p.notional)}</td>
                    <td>{num(p.qty, 6)}</td>
                    <td>{num(p.price)}</td>
                  </tr>
                ))
              ) : (
                <tr><td colSpan={5} className="muted">{t("strat.flat")}</td></tr>
              )}
            </tbody>
          </table>
        </div>
        <div>
          <h4>{t("strat.fills", { fee: fmtFixed((data?.cost.taker ?? 0) * 100, 2) })}</h4>
          <table className="num">
            <thead>
              <tr><th>{t("strat.col.day")}</th><th>{t("perf.a.coin")}</th><th>{t("ms.col.side")}</th><th>{t("strat.col.amount")}</th><th>{t("strat.col.fillPx")}</th><th>{t("strat.col.feeSlip")}</th></tr>
            </thead>
            <tbody>
              {data?.fills.length ? (
                data.fills.slice(0, 12).map((f) => (
                  <tr key={`${f.day}-${f.coin}`}>
                    <td>{fmtDate(f.day, { timeZone: "UTC", month: "2-digit", day: "2-digit" })}</td>
                    <td>{f.coin}</td>
                    <td className={f.side === "buy" ? "up" : "down"}>{f.side === "buy" ? t("chart.buy") : t("chart.sell")}</td>
                    <td>{num(f.notional)}</td>
                    <td>{num(f.fill_price)}</td>
                    <td>{num(f.fee + f.slippage, 3)}</td>
                  </tr>
                ))
              ) : (
                <tr><td colSpan={6} className="muted">{t("common.none")}</td></tr>
              )}
            </tbody>
          </table>
        </div>
      </div>
    </section>
  );
}

function Kpi({ label, value, tone: t, sub }: { label: string; value: string; tone?: string; sub?: string }) {
  return (
    <div className="console-stat">
      <span>{label}</span>
      <strong className={t || ""}>{value}</strong>
      {sub ? <small className="muted">{sub}</small> : null}
    </div>
  );
}

function UniverseNote({ data }: { data: StrategySummary }) {
  const u = data.universe;
  const cur = u.current || [];
  const conf = u.configured || [];
  const avail = Object.keys(u.coverage || {});
  const last = u.changes[u.changes.length - 1];
  const pending = cur.length > 0 && avail.length > 0 && (cur.length !== avail.length || avail.some((c) => !cur.includes(c)));
  const nextDay = data.status.nextDueDay;
  const unavailable = Object.entries(u.unavailable || {});
  const { t } = useTranslation();
  return (
    <div className="strat-universe">
      <div>
        <b>{t("strat.u.pool")}</b>: {t("strat.u.current", { n: cur.length, list: cur.join(" ") })}
        {conf.length ? <> · {t("strat.u.research", { n: conf.length, avail: avail.length })}</> : null}
      </div>
      {last ? (
        <div>
          {t("strat.u.changed", { day: last.day, from: last.prev.length, to: last.coins.length })}
        </div>
      ) : pending ? (
        <div>
          {t("strat.u.pending", { day: nextDay, n: avail.length })}
        </div>
      ) : null}
      {unavailable.length ? <div className="down">{t("strat.u.missing")} {unavailable.map(([c, why]) => `${c} (${why})`).join("; ")}</div> : null}
      <div className="strat-warn">⚠ {t("strat.u.survivorship")}</div>
    </div>
  );
}

// i18n keys strat.rule.r1..r9 (display only; the limits themselves live in the API)
const RISK_RULES = ["r1", "r2", "r3", "r4", "r5", "r6", "r7", "r8", "r9"];

function RiskBox({ risk }: { risk: StrategyRisk }) {
  const m = risk.lastMark;
  const f = risk.todayFlags || {};
  const lock = risk.locked && risk.lock;
  const { t } = useTranslation();
  const flags = [f.stop_new ? t("strat.flag.stopNew") : "", f.halve ? t("strat.flag.halved") : "", f.flat ? t("strat.flag.flat") : ""].filter(Boolean);
  const ev = risk.events || [];
  return (
    <div className="strat-risk">
      <div className="strat-risk-head">
        <b>{t("strat.risk.title")}</b>
        <span className={`strat-risk-state ${lock || risk.dataBad ? "down" : "up"}`}>
          {lock
            ? risk.lock?.kind === "review"
              ? `🔒 ${t("strat.risk.lockReview", { why: risk.lock?.reason })}`
              : `🔒 ${t("strat.risk.lockUntil", { time: SH.format(new Date(risk.lock?.until || 0)), why: risk.lock?.reason })}`
            : risk.dataBad
              ? `⚠ ${t("strat.risk.dataBad", { why: risk.dataBad.reason })}`
              : t("strat.risk.ok")}
        </span>
        {m ? (
          <span className="muted">
            {t("strat.risk.mark", { time: SH.format(new Date(m.ts)), day: pct(m.dayRet), dd: pct(m.drawdown) })}
            {flags.length ? ` · ${t("strat.risk.today", { list: flags.join(", ") })}` : ""}
          </span>
        ) : (
          <span className="muted">{t("strat.risk.noMark")}</span>
        )}
      </div>
      <div className="strat-risk-rules">{RISK_RULES.map((r) => <span key={r}>{t(`strat.rule.${r}`)}</span>)}</div>
      <table className="num strat-risk-events">
        <thead>
          <tr><th>{t("sec.colTime")}</th><th>{t("strat.risk.trigger")}</th><th>{t("strat.risk.action")}</th><th>{t("strat.risk.value")}</th><th>{t("strat.risk.when")}</th></tr>
        </thead>
        <tbody>
          {ev.length ? (
            ev.slice(0, 10).map((e, i) => (
              <tr key={`${e.ts}-${e.kind}-${i}`}>
                <td>{SH.format(new Date(e.ts))}</td>
                <td>{tx("kind", e.kind, e.label)}</td>
                <td>{tx("action", e.action)}</td>
                <td>{e.value == null ? "—" : e.kind.startsWith("cap") ? `${fmtFixed(e.value * 100, 1)}%` : pct(e.value, e.kind === "funding" ? 3 : 2)}</td>
                <td>{e.at === "close" ? t("strat.risk.atClose") : e.at === "intraday" ? t("strat.risk.atIntraday") : e.at}</td>
              </tr>
            ))
          ) : (
            <tr><td colSpan={5} className="muted">{t("strat.risk.noEvents", { n: risk.eventCount ?? 0 })}</td></tr>
          )}
        </tbody>
      </table>
      {risk.backtestNote ? <div className="muted strat-risk-note">{t("strat.risk.backtestNote")}</div> : null}
    </div>
  );
}

const bp = (v: number | null | undefined, d = 1) => (v == null || !Number.isFinite(v) ? "—" : `${v > 0 ? "+" : ""}${fmtFixed(v, d)} bp`);

function ExecShadowBox({ s }: { s: ExecShadowSummary | null }) {
  const { t } = useTranslation();
  return (
    <div className="strat-risk strat-exec">
      <div className="strat-risk-head">
        <b>{t("strat.exec.title")}</b>
        <span className="muted">{t("strat.exec.sub")}</span>
        {s && s.n ? (
          <span className={(s.notionalWeightedDeviationBp ?? 0) > 0 ? "down" : "up"}>
            {t("strat.exec.weighted", { actual: bp(s.notionalWeightedShortfallMidBp), dev: bp(s.notionalWeightedDeviationBp) })}
          </span>
        ) : null}
      </div>
      {s && s.coins.length ? (
        <table className="num strat-risk-events">
          <thead>
            <tr><th>{t("perf.a.coin")}</th><th>{t("strat.exec.n")}</th><th>{t("ob.spread")}</th><th>{t("strat.exec.shortfall")}</th><th>{t("strat.exec.assumed")}</th><th>{t("strat.exec.dev")}</th><th>{t("strat.exec.vsClose")}</th></tr>
          </thead>
          <tbody>
            {s.coins.map((c) => (
              <tr key={c.coin}>
                <td>{c.coin}</td>
                <td>{c.n}</td>
                <td>{bp(c.spreadBp, 2)}</td>
                <td>{bp(c.shortfallMidBp, 2)}</td>
                <td>{bp(c.assumedBp, 1)}</td>
                <td className={c.deviationBp > 0 ? "down" : "up"}>{bp(c.deviationBp, 2)}</td>
                <td>{bp(c.shortfallCloseBp, 1)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <div className="muted">{t("strat.exec.empty")}{s?.error ? ` · ${s.error}` : ""}</div>
      )}
      {s ? <div className="muted strat-risk-note">{t("strat.exec.note")}{s.skipped ? ` ${t("strat.exec.skipped", { n: s.skipped })}` : ""}{s.errors ? ` ${t("strat.exec.errors", { n: s.errors })}` : ""}</div> : null}
    </div>
  );
}

const bpct = (v: number | null | undefined, d = 2) => (v == null || !Number.isFinite(v) ? "—" : `${v > 0 ? "+" : ""}${fmtFixed(v * 100, d)}%`);
const short = (x: string | null | undefined) => (x ? x.slice(0, 10) : "—");

function BandBox({ b }: { b: ExpectedBand | null }) {
  if (!b || b.status === "no_band") return null;
  const curve = b.curve ?? [];
  const W = 320, H = 120;
  const ys = curve.flatMap((c) => [c.p05, c.p95, c.paper ?? 0]);
  const lo = Math.min(0, ...ys), hi = Math.max(0, ...ys);
  const x = (n: number) => ((n - 1) / Math.max(1, curve.length - 1)) * W;
  const y = (v: number) => H - ((v - lo) / (hi - lo || 1)) * H;
  const area = curve.length
    ? `M${curve.map((c) => `${x(c.n).toFixed(1)},${y(c.p95).toFixed(1)}`).join("L")}L${[...curve].reverse().map((c) => `${x(c.n).toFixed(1)},${y(c.p05).toFixed(1)}`).join("L")}Z`
    : "";
  const mid = curve.map((c, i) => `${i ? "L" : "M"}${x(c.n).toFixed(1)},${y(c.p50).toFixed(1)}`).join("");
  const paper = curve.filter((c) => c.paper != null).map((c, i) => `${i ? "L" : "M"}${x(c.n).toFixed(1)},${y(c.paper as number).toFixed(1)}`).join("");
  const bad = b.status === "below" || b.status === "dd_breach";
  const { t } = useTranslation();
  return (
    <div className="strat-risk strat-band">
      <div className="strat-risk-head">
        <b>{t("strat.band.title")}</b>
        <span className={bad ? "down" : b.status === "above" ? "warn" : "up"}>{tx("band", b.status, b.label ?? b.status)}</span>
        {b.n ? (
          <span className="muted">
            {t("strat.band.stats", { n: b.n, cum: bpct(b.cum), p05: bpct(b.p05), p95: bpct(b.p95), mdd: bpct(b.mdd), worst: bpct(b.mddP05) })}
          </span>
        ) : null}
      </div>
      {curve.length ? (
        <svg viewBox={`0 0 ${W} ${H}`} className="strat-band-svg" preserveAspectRatio="none" role="img" aria-label={t("strat.band.aria")}>
          <path d={area} fill="currentColor" opacity={0.12} />
          <line x1={0} x2={W} y1={y(0)} y2={y(0)} stroke="currentColor" opacity={0.25} strokeDasharray="3 3" />
          <path d={mid} fill="none" stroke="currentColor" opacity={0.4} strokeWidth={1} />
          {paper ? <path d={paper} fill="none" stroke="#2f80ed" strokeWidth={2} /> : null}
        </svg>
      ) : null}
      <div className="muted strat-risk-note">
        {t("strat.band.note")} {t("strat.band.meta", { name: b.name, v: b.version, seed: b.registered?.seed, block: b.registered?.block, paths: b.registered?.n_paths, src: b.sourceSha })}
        {b.error ? ` · ${b.error}` : ""}
      </div>
    </div>
  );
}

function VersionLine({ v }: { v: RunVersion }) {
  const l = v.lastRun;
  const { t } = useTranslation();
  return (
    <div className="muted strat-version">
      {t("strat.ver.line", { commit: short(l.git_commit), params: short(l.params_sha), cost: short(l.cost_model_sha) })}
      {v.unversionedRuns ? ` · ${t("strat.ver.unversioned", { n: v.unversionedRuns })}` : ""}
      {v.changedSinceLastRun ? ` · ⚠ ${t("strat.ver.changed")}` : ""}
      {l.git_commit && v.current.git_commit && l.git_commit !== v.current.git_commit ? ` · ${t("strat.ver.current", { c: short(v.current.git_commit) })}` : ""}
    </div>
  );
}
