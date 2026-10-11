import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { marketProvider } from "@/providers/HttpWsProvider";
import type { PnlCurve, PnlSummary } from "@/types/contracts";
import { errText } from "@/i18n/errors";
import { fmtDate, fmtFixed, fmtNum, fmtSignedPct } from "@/i18n/format";
import { Empty, Sk } from "@/components/ui/Skeleton";
import { tone, usePnlSummary } from "@/pages/PnlPage/usePnl";

type Range = "7d" | "30d" | "all";
const RANGES: Range[] = ["7d", "30d", "all"];

export const money = (v: number) => fmtNum(v, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
export const signed = (v: number) => `${v > 0 ? "+" : v < 0 ? "−" : ""}${money(Math.abs(v))}`;

function px(v: number) {
  const a = Math.abs(v);
  return fmtFixed(v, a >= 1000 ? 2 : a >= 1 ? 4 : 6);
}

function qty(v: number) {
  return fmtNum(v, { maximumFractionDigits: v >= 1 ? 4 : 8 });
}

function age(ms: number | null, t: (k: string, o?: Record<string, unknown>) => string) {
  if (ms == null) return t("pnl.noPrice");
  const s = Math.round(ms / 1000);
  return s < 90 ? t("pnl.ageSec", { n: s }) : t("pnl.ageMin", { n: Math.round(s / 60) });
}

/** Equity line (SVG). Colour follows the period's sign, so it respects 红涨绿跌 / 绿涨红跌. */
function EquityChart({ c }: { c: PnlCurve }) {
  const { t } = useTranslation();
  const [hover, setHover] = useState<number | null>(null);
  const W = 800;
  const H = 220;
  const pad = 6;
  const pts = c.points;
  const geo = useMemo(() => {
    if (pts.length < 2) return null;
    const t0 = pts[0].ts;
    const t1 = pts[pts.length - 1].ts;
    let lo = Math.min(...pts.map((p) => p.equity));
    let hi = Math.max(...pts.map((p) => p.equity));
    if (hi - lo < 1e-6) {
      lo -= 1;
      hi += 1;
    }
    const x = (ts: number) => pad + ((ts - t0) / Math.max(1, t1 - t0)) * (W - 2 * pad);
    const y = (v: number) => pad + (1 - (v - lo) / (hi - lo)) * (H - 2 * pad);
    const line = pts.map((p, i) => `${i ? "L" : "M"}${x(p.ts).toFixed(1)},${y(p.equity).toFixed(1)}`).join("");
    return { x, y, line, area: `${line}L${x(t1).toFixed(1)},${H}L${x(t0).toFixed(1)},${H}Z`, lo, hi, base: y(pts[0].equity) };
  }, [pts]);
  if (!geo) return <Empty title={t("pnl.noCurve")} />;
  const cls = tone(c.change) || "flat";
  const h = hover == null ? null : pts[hover];
  return (
    <div className="pnl-chart-wrap">
      <svg
        className={`pnl-chart ${cls}`}
        viewBox={`0 0 ${W} ${H}`}
        preserveAspectRatio="none"
        role="img"
        aria-label={t("pnl.curve")}
        onMouseLeave={() => setHover(null)}
        onMouseMove={(e) => {
          const r = (e.currentTarget as SVGSVGElement).getBoundingClientRect();
          const rel = ((e.clientX - r.left) / r.width) * W;
          let best = 0;
          pts.forEach((p, i) => {
            if (Math.abs(geo.x(p.ts) - rel) < Math.abs(geo.x(pts[best].ts) - rel)) best = i;
          });
          setHover(best);
        }}
      >
        <line x1="0" x2={W} y1={geo.base} y2={geo.base} className="pnl-base" />
        <path d={geo.area} className="pnl-area" />
        <path d={geo.line} className="pnl-line" vectorEffect="non-scaling-stroke" />
        {h ? <line x1={geo.x(h.ts)} x2={geo.x(h.ts)} y1="0" y2={H} className="pnl-cursor" vectorEffect="non-scaling-stroke" /> : null}
      </svg>
      <div className="pnl-chart-legend num">
        {h ? (
          <>
            <span>{fmtDate(h.ts, { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false })}</span>
            <b>{money(h.equity)} USDT</b>
            <span className={tone(h.equity - c.start)}>{signed(h.equity - c.start)}</span>
          </>
        ) : (
          <>
            <span>{t("pnl.rangeChange")}</span>
            <b className={tone(c.change)}>{signed(c.change)} USDT</b>
            <span className={tone(c.change)}>{fmtSignedPct(c.changePct)}</span>
          </>
        )}
      </div>
      <div className="pnl-axis num">
        <span>{money(geo.hi)}</span>
        <span>{money(geo.lo)}</span>
      </div>
    </div>
  );
}

function Card({ label, value, sub, cls = "", big = false }: { label: string; value: string; sub?: string; cls?: string; big?: boolean }) {
  return (
    <div className={`pnl-card${big ? " is-big" : ""}`}>
      <small>{label}</small>
      <b className={`num ${cls}`}>{value}</b>
      {sub ? <em className={`num ${cls}`}>{sub}</em> : null}
    </div>
  );
}

export function PnlCards({ s }: { s: PnlSummary }) {
  const { t } = useTranslation();
  return (
    <div className="pnl-cards">
      <Card big label={t("pnl.equity")} value={`${money(s.equity)}`} sub={t("pnl.cashLine", { cash: money(s.cash) })} />
      <Card label={t("pnl.today")} value={s.today == null ? "—" : signed(s.today)} sub={s.todayPct == null ? undefined : fmtSignedPct(s.todayPct)} cls={tone(s.today)} />
      <Card label={t("pnl.unrealized")} value={signed(s.unrealized)} cls={tone(s.unrealized)} />
      <Card label={t("pnl.realized")} value={signed(s.realized)} cls={tone(s.realized)} />
      <Card label={t("pnl.totalReturn")} value={fmtSignedPct(s.totalReturn)} sub={signed(s.totalPnl)} cls={tone(s.totalPnl)} />
    </div>
  );
}

export function PnlPage() {
  const { t } = useTranslation();
  const { data: s, error, needLogin } = usePnlSummary(10_000);
  const [range, setRange] = useState<Range>("7d");
  const [curve, setCurve] = useState<PnlCurve | null>(null);
  const [curveErr, setCurveErr] = useState("");

  useEffect(() => {
    let alive = true;
    setCurve(null);
    const load = () =>
      marketProvider
        .getPnlCurve(range)
        .then((c) => alive && (setCurve(c), setCurveErr("")))
        .catch((e: unknown) => alive && setCurveErr(errText(e, "common.loadFailed")));
    load();
    const id = window.setInterval(() => !document.hidden && load(), 60_000);
    return () => {
      alive = false;
      window.clearInterval(id);
    };
  }, [range]);

  if (needLogin) {
    return (
      <div className="shell-page pro-page pnl-page">
        <header className="pro-head"><h1>{t("pnl.title")}</h1></header>
        <div className="pro-alert">{t("pnl.loginFirst")} <Link className="td-link" to="/login?next=/pnl">{t("pnl.login")}</Link></div>
      </div>
    );
  }

  return (
    <div className="shell-page pro-page pnl-page">
      <header className="pro-head pnl-head">
        <div>
          <h1>{t("pnl.title")}</h1>
          <p>{t("pnl.sub")}</p>
        </div>
        <Link className="td-link pnl-perf-link" to="/performance">{t("pnl.strategyLink")} ›</Link>
      </header>
      {error && !s ? <div className="pro-alert">{errText(error, "common.loadFailed")}</div> : null}
      {s?.stale ? (
        <div className="pro-alert is-warn pnl-stale">
          {s.priceMissing ? t("pnl.priceMissingNote") : t("pnl.staleNote", { min: Math.round(s.staleAfterMs / 60000) })}
        </div>
      ) : null}
      <section className="pnl-section">{s ? <PnlCards s={s} /> : <Sk w={320} h={60} />}</section>

      <section className="pnl-section pane">
        <div className="pnl-sec-head">
          <h2>{t("pnl.curve")}</h2>
          <div className="mk-tabs" role="tablist" aria-label={t("pnl.curve")}>
            {RANGES.map((r) => (
              <button key={r} type="button" role="tab" aria-selected={range === r} className={range === r ? "is-on" : ""} onClick={() => setRange(r)}>
                {t(`pnl.range.${r}`)}
              </button>
            ))}
          </div>
        </div>
        {curveErr ? <div className="pro-alert">{curveErr}</div> : curve ? <EquityChart c={curve} /> : <Sk w={320} h={180} />}
        {curve?.approx ? <p className="td-note">{t("pnl.approxNote")}</p> : null}
      </section>

      <section className="pnl-section pane">
        <div className="pnl-sec-head">
          <h2>{t("pnl.positions", { n: s?.positions.length ?? 0 })}</h2>
          {s ? <span className="muted num">{t("pnl.updated", { time: fmtDate(s.asOf, { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false }) })}</span> : null}
        </div>
        {!s ? (
          <Sk w={320} h={40} />
        ) : s.positions.length === 0 ? (
          <Empty title={t("pnl.noPositions")} action={<Link className="td-link" to="/?symbol=BTC">{t("pnl.goTrade")} ›</Link>} />
        ) : (
          <div className="pro-table-wrap">
            <table className="pro-table pnl-table">
              <thead>
                <tr>
                  <th>{t("pnl.col.symbol")}</th>
                  <th>{t("pnl.col.qty")}</th>
                  <th>{t("pnl.col.avg")}</th>
                  <th>{t("pnl.col.last")}</th>
                  <th>{t("pnl.col.value")}</th>
                  <th>{t("pnl.col.pnl")}</th>
                  <th>{t("pnl.col.pct")}</th>
                </tr>
              </thead>
              <tbody>
                {s.positions.map((p) => (
                  <tr key={p.symbol}>
                    <td>
                      <Link className="pnl-sym" to={`/?symbol=${p.symbol}`}>{p.symbol}</Link>
                      <span className="muted"> /USDT</span>
                    </td>
                    <td className="num">{qty(p.qty)}</td>
                    <td className="num">{px(p.avgPrice)}</td>
                    <td className="num">
                      {px(p.last)}
                      {p.stale ? <span className="pnl-stale-tag" title={age(p.priceAgeMs, t)}>{p.priceMissing ? t("pnl.costTag") : t("pnl.staleTag")}</span> : null}
                    </td>
                    <td className="num">{money(p.value)}</td>
                    <td className={`num ${tone(p.unrealized)}`}>{signed(p.unrealized)}</td>
                    <td className={`num ${tone(p.unrealized)}`}>{fmtSignedPct(p.unrealizedPct)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <p className="td-note">{t("pnl.footnote")}</p>
      </section>
    </div>
  );
}
