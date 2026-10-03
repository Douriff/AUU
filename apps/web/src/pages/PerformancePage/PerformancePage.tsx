import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { marketProvider } from "@/providers/HttpWsProvider";
import type { StrategyReport } from "@/types/mainstream";
import { Empty, Sk, SkCards } from "@/components/ui/Skeleton";

/** P1-1/P1-2: strategy performance report (paper ledger). Login is enforced by AppShell + the API (401). */

const pct = (v: number | null | undefined, d = 2) =>
  v == null || !Number.isFinite(v) ? "—" : `${v > 0 ? "+" : ""}${(v * 100).toFixed(d)}%`;
const fx = (v: number | null | undefined, d = 2) => (v == null || !Number.isFinite(v) ? "—" : v.toFixed(d));
const usd = (v: number | null | undefined) =>
  v == null || !Number.isFinite(v) ? "—" : `${v > 0 ? "+" : ""}${v.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
const MONTHS = ["1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "11", "12"];

function heat(v: number): string {
  const a = Math.min(1, Math.abs(v) / 0.08);
  return `color-mix(in srgb, var(${v >= 0 ? "--up" : "--down"}) ${Math.round(14 + 56 * a)}%, transparent)`;
}
const tn = (v: number | null | undefined) => (v == null || !Number.isFinite(v) || v === 0 ? "" : v > 0 ? "up" : "down");

export function PerformancePage() {
  const [rep, setRep] = useState<StrategyReport | null>(null);
  const [err, setErr] = useState("");

  useEffect(() => {
    let alive = true;
    marketProvider
      .getStrategyReport()
      .then((d) => alive && (setRep(d), setErr("")))
      .catch((e) => alive && setErr(String(e?.message || e)));
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
        <h1>策略绩效</h1>
        <p>{rep ? `${rep.strategy} · 纸面账本 · 截至 ${rep.asOf ?? "—"} 收盘 · 起始 ${rep.startNav.toLocaleString()} USDT` : <Sk w={260} h={11} />}</p>
        <Link to="/console" className="pro-head-link">策略控制台 ›</Link>
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
          <b>Go/No-Go：{g.verdict === "go" ? "Go" : g.verdict === "no-go" ? "No-Go" : "待评估"}</b>
          <span>{g.message}</span>
          <span className="muted">
            日收益年化 95% CI：{rep?.ci ? `${pct(rep.ci.lo)} ~ ${pct(rep.ci.hi)}` : `样本不足 30 天（${m?.days ?? 0} 天）`} · 判定标准：≥{g.minDays} 天、CI 下限 &gt; 0 且跑赢国债
          </span>
        </section>
      ) : null}

      {m && m.days ? (
        <section className="perf-kpis">
          {[
            ["累计收益", pct(m.totalReturn)],
            ["年化（CAGR）", m.shortSample ? "—" : pct(m.cagr)],
            ["Sortino", m.shortSample ? "—" : fx(m.sortino)],
            ["Calmar", m.shortSample ? "—" : fx(m.calmar)],
            ["Sharpe", m.shortSample ? "—" : fx(m.sharpe)],
            ["最大回撤", `${pct(m.maxDrawdown)}${m.maxDrawdownDay ? ` · ${m.maxDrawdownDay}` : ""}`],
            ["最长回撤", `${m.longestDrawdownDays ?? 0} 天`],
            ["当前回撤", `${pct(m.currentDrawdown)} · ${m.currentDrawdownDays ?? 0} 天`],
            ["日胜率", pct(m.winRate, 1)],
            ["日盈亏比", fx(m.winLossRatio)],
            ["最好 / 最差日", `${pct(m.bestDay)} / ${pct(m.worstDay)}`],
            ["天数", String(m.days)],
          ].map(([k, v]) => (
            <div key={k} className="perf-kpi">
              <span className="muted">{k}</span>
              <b className={k === "累计收益" ? tn(m.totalReturn) : k === "年化（CAGR）" && !m.shortSample ? tn(m.cagr) : k === "最大回撤" ? tn(m.maxDrawdown) : k === "当前回撤" ? tn(m.currentDrawdown) : ""}>{v}</b>
            </div>
          ))}
          {m.shortSample ? <p className="muted perf-warn">样本少于 30 天：年化、Sortino、Calmar、Sharpe 没有统计意义，暂不显示（满 30 天后自动出现）。</p> : null}
        </section>
      ) : rep ? (
        <Empty icon="chart" title="尚无纸面记录" hint="策略完成第一次调仓（北京时间 08:00）后开始生成绩效" />
      ) : null}

      {years.length ? (
        <section className="perf-card">
          <h2>月度收益</h2>
          <div className="perf-scroll">
            <table className="perf-heat num">
              <thead>
                <tr>
                  <th>年</th>
                  {MONTHS.map((x) => (
                    <th key={x}>{x}月</th>
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
                        <td key={x} style={c ? { background: heat(c.ret) } : undefined} title={c ? `${c.days} 天` : ""}>
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
          <h2>逐币归因（USDT）</h2>
          <div className="perf-scroll">
            <table className="perf-attr num">
              <thead>
                <tr>
                  <th>币</th>
                  <th>价格</th>
                  <th>资金费</th>
                  <th>成本</th>
                  <th>合计</th>
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
                  <td colSpan={4}>账本实际盈亏</td>
                  <td>{usd(rep.attribution.totalUsd)}</td>
                </tr>
                <tr>
                  <td colSpan={4}>未归因残差</td>
                  <td>{usd(rep.attribution.residualUsd)}</td>
                </tr>
              </tfoot>
            </table>
          </div>
          <p className="muted perf-note">
            价格 = 前一日权重 × 当日涨跌；资金费 = −权重 × 当日资金费率（多头付正费率）；成本 = 手续费 + 滑点（含风控盘中调整成交）。
            {rep.attribution.segmentsFromAdjustments ? ` 有 ${rep.attribution.segmentsFromAdjustments} 次盘中风控调整，已分段累加。` : ""}
            {rep.attribution.fundingByCoin ? "" : " 资金费无法逐币取得，已按账本总额计入，不分币。"}
          </p>
        </section>
      ) : null}
      {rep ? <p className="muted perf-note">{rep.note}</p> : null}
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
  return (
    <section className="perf-card">
      <h2>净值与回撤</h2>
      <svg viewBox={`0 0 ${W} ${HN}`} className="perf-svg" preserveAspectRatio="none" role="img" aria-label="净值">
        <line x1={0} x2={W} y1={yn(rep.startNav)} y2={yn(rep.startNav)} stroke="currentColor" opacity={0.25} strokeDasharray="4 4" />
        <path d={nav} fill="none" stroke="#f0a531" strokeWidth={2} vectorEffect="non-scaling-stroke" />
      </svg>
      <svg viewBox={`0 0 ${W} ${H}`} className="perf-svg perf-dd" preserveAspectRatio="none" role="img" aria-label="回撤">
        <path d={dd} className="dd-area" strokeWidth={1} vectorEffect="non-scaling-stroke" />
      </svg>
      <p className="muted perf-note">
        {pts[0].day} ~ {pts[pts.length - 1].day} · 最大回撤 {pct(m.maxDrawdown)} · 最长回撤 {m.longestDrawdownDays ?? 0} 天
        {m.longestDrawdown ? `（${m.longestDrawdown.from} ~ ${m.longestDrawdown.to}）` : ""} · 回撤坐标底部 = {pct(minDd)}
      </p>
    </section>
  );
}
