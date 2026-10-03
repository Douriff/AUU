import { useEffect, useRef, useState } from "react";
import { ColorType, createChart, type IChartApi, type ISeriesApi, type UTCTimestamp } from "lightweight-charts";
import { Link } from "react-router-dom";
import { marketProvider } from "@/providers/HttpWsProvider";
import { SkCards } from "@/components/ui/Skeleton";
import { CHART_CHROME, chartColors, onColorPref } from "@/theme/colorPref";
import type { ExecShadowSummary, ExpectedBand, RunVersion, StrategyRisk, StrategySummary } from "@/types/mainstream";

/** M3: daily paper runner (trend_tsmom_v1) — equity vs BTC buy&hold vs T-bill, daily returns, positions. */

const POLL_MS = 60_000;
const COL = { strat: "#f0a531", btc: "#7d8fb3", tbill: "#5f6672" };

const pct = (v: number | null | undefined, d = 2) =>
  v == null || !Number.isFinite(v) ? "—" : `${v > 0 ? "+" : ""}${(v * 100).toFixed(d)}%`;
const num = (v: number | null | undefined, d = 2) =>
  v == null || !Number.isFinite(v) ? "—" : v.toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
const tone = (v: number | null | undefined) => (v == null || !v ? "flat" : v > 0 ? "up" : "down");
const SH = new Intl.DateTimeFormat("zh-CN", { timeZone: "Asia/Shanghai", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false });

export function StrategyPanel() {
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
        .catch((e) => alive && setErr(String(e?.message || e)));
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
    });
    const fmt = { type: "custom" as const, formatter: (v: number) => `${v > 0 ? "+" : ""}${v.toFixed(2)}%` };
    series.current = {
      s: c.addLineSeries({ color: COL.strat, lineWidth: 2, priceFormat: fmt, title: "策略" }),
      b: c.addLineSeries({ color: COL.btc, lineWidth: 1, priceFormat: fmt, title: "BTC" }),
      t: c.addLineSeries({ color: COL.tbill, lineWidth: 1, lineStyle: 2, priceFormat: fmt, title: "国债" }),
      r: c.addHistogramSeries({ priceScaleId: "ret", priceFormat: fmt, lastValueVisible: false, priceLineVisible: false }),
    };
    c.priceScale("ret").applyOptions({ scaleMargins: { top: 0.75, bottom: 0 } });
    chart.current = c;
    return () => {
      c.remove();
      chart.current = null;
    };
  }, []);

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
    <section className="console-card strat" aria-label="趋势策略纸面">
      <header className="console-card-bar strat-head">
        <div className="console-title">趋势策略 · {data?.strategy.name || "trend_tsmom_v1"}</div>
        <span className="strat-sub">每日 08:00 调仓</span>
        <Link to="/performance" className="console-badge strat-report-link">绩效报告 →</Link>
        {g ? (
          <span className={`console-go lamp-${g.lamp}`} title={g.message}>
            {g.verdict === "go" ? "Go" : g.verdict === "no-go" ? "No-Go" : "待评估"}
          </span>
        ) : null}
      </header>
      {err ? <p className="console-err">{err}</p> : null}
      {!data && !err ? <SkCards n={8} h={62} /> : null}
      <div className="strat-kpis" hidden={!data}>
        <Kpi label="权益 USDT" value={num(data?.nav)} />
        <Kpi label="策略累计" value={pct(data?.totals.strategy)} tone={tone(data?.totals.strategy)} />
        <Kpi label="BTC 买入持有" value={pct(data?.totals.btc)} tone={tone(data?.totals.btc ?? 0)} />
        <Kpi label="国债（年化 3.99%）" value={pct(data?.totals.tbill)} />
        <Kpi label="最近日收益" value={pct(last?.ret, 3)} tone={tone(last?.ret)} />
        <Kpi label="总敞口" value={last ? `${(last.gross * 100).toFixed(1)}%` : "—"} />
        <Kpi
          label="上次调仓"
          value={st?.lastDay ? `${st.lastDay} 收盘` : "尚未调仓"}
          sub={st?.lastRunAt ? `执行于 ${SH.format(new Date(st.lastRunAt))}` : st?.waiting || ""}
        />
        <Kpi
          label="调仓状态"
          value={st ? (st.stalled ? `停滞 ${st.hoursSinceRebalance.toFixed(1)}h` : `正常 · ${st.hoursSinceRebalance.toFixed(1)}h 前`) : "—"}
          tone={st?.stalled ? "bad" : "ok"}
          sub={st ? `已运行 ${st.days} 天 · 超过 ${st.stallHours}h 报警` : ""}
        />
      </div>
      {g ? <p className={`strat-go lamp-${g.lamp}`}>{g.message}（标准：日收益 ≥ {g.minDays} 天，bootstrap CI 下限 &gt; 0 且跑赢国债）</p> : null}
      {data ? <UniverseNote data={data} /> : null}
      {data?.risk?.enabled ? <RiskBox risk={data.risk} /> : null}
      <BandBox b={data?.expectedBand ?? null} />
      <ExecShadowBox s={data?.execShadow ?? null} />
      {data?.version ? <VersionLine v={data.version} /> : null}
      <div className="strat-legend">
        <i style={{ background: COL.strat }} />策略 <i style={{ background: COL.btc }} />BTC 买入持有 <i style={{ background: COL.tbill }} />国债 <i className="bar" />日收益
      </div>
      <div className="strat-chart" ref={host} />
      <div className="strat-tables">
        <div>
          <h4>持仓（{data?.positions.length ?? 0}）</h4>
          <table className="num">
            <thead>
              <tr><th>币</th><th>权重</th><th>市值</th><th>数量</th><th>收盘价</th></tr>
            </thead>
            <tbody>
              {data?.positions.length ? (
                data.positions.map((p) => (
                  <tr key={p.coin}>
                    <td>{p.coin}</td>
                    <td>{(p.weight * 100).toFixed(2)}%</td>
                    <td>{num(p.notional)}</td>
                    <td>{num(p.qty, 6)}</td>
                    <td>{num(p.price)}</td>
                  </tr>
                ))
              ) : (
                <tr><td colSpan={5} className="muted">空仓（信号为负或尚未调仓）</td></tr>
              )}
            </tbody>
          </table>
        </div>
        <div>
          <h4>调仓成交（费 {(data?.cost.taker ?? 0) * 100}% + 滑点）</h4>
          <table className="num">
            <thead>
              <tr><th>日</th><th>币</th><th>方向</th><th>金额</th><th>成交价</th><th>费+滑点</th></tr>
            </thead>
            <tbody>
              {data?.fills.length ? (
                data.fills.slice(0, 12).map((f) => (
                  <tr key={`${f.day}-${f.coin}`}>
                    <td>{new Date(f.day).toISOString().slice(5, 10)}</td>
                    <td>{f.coin}</td>
                    <td className={f.side === "buy" ? "up" : "down"}>{f.side === "buy" ? "买" : "卖"}</td>
                    <td>{num(f.notional)}</td>
                    <td>{num(f.fill_price)}</td>
                    <td>{num(f.fee + f.slippage, 3)}</td>
                  </tr>
                ))
              ) : (
                <tr><td colSpan={6} className="muted">暂无</td></tr>
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
  return (
    <div className="strat-universe">
      <div>
        <b>币池</b>：当前 {cur.length} 个（{cur.join(" ")}）
        {conf.length ? <> · 研究币池 {conf.length} 个，已有数据 {avail.length} 个</> : null}
      </div>
      {last ? (
        <div>
          切换记录：{last.day} 收盘起 {last.prev.length} → {last.coins.length} 个币（之前的账本历史保持原样）
        </div>
      ) : pending ? (
        <div>
          待切换：从 {nextDay} 收盘（次日北京时间 08:00 调仓）起改用 {avail.length} 个币；之前的账本历史不重算
        </div>
      ) : null}
      {unavailable.length ? <div className="down">缺少数据：{unavailable.map(([c, why]) => `${c}（${why}）`).join("；")}</div> : null}
      <div className="strat-warn">⚠ 幸存者偏差：{u.survivorship}</div>
    </div>
  );
}

const RISK_RULES = [
  "总敞口 ≤ 1x 权益",
  "单币 ≤ 25%",
  "当日 −3% 停止新开仓",
  "当日 −5% 全部减半",
  "当日 −8% 全部平仓并锁 24h",
  "回撤 −15% 仓位减半",
  "回撤 −20% 清仓复查",
  "资金费：多头 > 0.1%/8h 减仓",
  "数据：1h 标记价超过 2 根未更新或交易所异常 → 只减不开",
];

function RiskBox({ risk }: { risk: StrategyRisk }) {
  const m = risk.lastMark;
  const f = risk.todayFlags || {};
  const lock = risk.locked && risk.lock;
  const flags = [f.stop_new ? "停止新开仓" : "", f.halve ? "已减半" : "", f.flat ? "已平仓" : ""].filter(Boolean);
  const ev = risk.events || [];
  return (
    <div className="strat-risk">
      <div className="strat-risk-head">
        <b>风控硬上限</b>
        <span className={`strat-risk-state ${lock || risk.dataBad ? "down" : "up"}`}>
          {lock
            ? risk.lock?.kind === "review"
              ? `🔒 已锁定，需人工复查（${risk.lock?.reason}）`
              : `🔒 锁定至 ${SH.format(new Date(risk.lock?.until || 0))}（${risk.lock?.reason}）`
            : risk.dataBad
              ? `⚠ 数据熔断：只减不开（${risk.dataBad.reason}）`
              : "正常，未触发"}
        </span>
        {m ? (
          <span className="muted">
            小时标记 {SH.format(new Date(m.ts))}：当日 {pct(m.dayRet)} · 回撤 {pct(m.drawdown)}
            {flags.length ? ` · 今日：${flags.join("、")}` : ""}
          </span>
        ) : (
          <span className="muted">小时标记：尚无（调仓后的下一个整点开始）</span>
        )}
      </div>
      <div className="strat-risk-rules">{RISK_RULES.map((r) => <span key={r}>{r}</span>)}</div>
      <table className="num strat-risk-events">
        <thead>
          <tr><th>时间（北京）</th><th>触发</th><th>动作</th><th>数值</th><th>场景</th></tr>
        </thead>
        <tbody>
          {ev.length ? (
            ev.slice(0, 10).map((e, i) => (
              <tr key={`${e.ts}-${e.kind}-${i}`}>
                <td>{SH.format(new Date(e.ts))}</td>
                <td>{e.label}</td>
                <td>{e.action}</td>
                <td>{e.value == null ? "—" : e.kind.startsWith("cap") ? `${(e.value * 100).toFixed(1)}%` : pct(e.value, e.kind === "funding" ? 3 : 2)}</td>
                <td>{e.at === "close" ? "收盘调仓" : e.at === "intraday" ? "盘中" : e.at}</td>
              </tr>
            ))
          ) : (
            <tr><td colSpan={5} className="muted">暂无触发记录（共 {risk.eventCount ?? 0} 条）</td></tr>
          )}
        </tbody>
      </table>
      {risk.backtestNote ? <div className="muted strat-risk-note">{risk.backtestNote}</div> : null}
    </div>
  );
}

const bp = (v: number | null | undefined, d = 1) => (v == null || !Number.isFinite(v) ? "—" : `${v > 0 ? "+" : ""}${v.toFixed(d)} bp`);

function ExecShadowBox({ s }: { s: ExecShadowSummary | null }) {
  return (
    <div className="strat-risk strat-exec">
      <div className="strat-risk-head">
        <b>执行价影子记录</b>
        <span className="muted">每次调仓时按成交金额读取永续公开盘口（不影响成交和账本）</span>
        {s && s.n ? (
          <span className={(s.notionalWeightedDeviationBp ?? 0) > 0 ? "down" : "up"}>
            按金额加权：实际 {bp(s.notionalWeightedShortfallMidBp)} vs 假设 · 偏差 {bp(s.notionalWeightedDeviationBp)}
          </span>
        ) : null}
      </div>
      {s && s.coins.length ? (
        <table className="num strat-risk-events">
          <thead>
            <tr><th>币</th><th>次数</th><th>价差</th><th>盘口成本（对中间价）</th><th>假设滑点</th><th>偏差</th><th>对收盘价</th></tr>
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
        <div className="muted">暂无记录（下一次调仓开始积累）{s?.error ? ` · ${s.error}` : ""}</div>
      )}
      {s ? <div className="muted strat-risk-note">{s.note}{s.skipped ? ` 补跑日跳过 ${s.skipped} 笔。` : ""}{s.errors ? ` 读取失败 ${s.errors} 笔。` : ""}</div> : null}
    </div>
  );
}

const bpct = (v: number | null | undefined, d = 2) => (v == null || !Number.isFinite(v) ? "—" : `${v > 0 ? "+" : ""}${(v * 100).toFixed(d)}%`);
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
  return (
    <div className="strat-risk strat-band">
      <div className="strat-risk-head">
        <b>纸面 vs 回测预期区间</b>
        <span className={bad ? "down" : b.status === "above" ? "warn" : "up"}>{b.label ?? b.status}</span>
        {b.n ? (
          <span className="muted">
            N={b.n} 天 · 累计 {bpct(b.cum)}（5%–95%：{bpct(b.p05)} ~ {bpct(b.p95)}）· 回撤 {bpct(b.mdd)}（5% 最差 {bpct(b.mddP05)}）
          </span>
        ) : null}
      </div>
      {curve.length ? (
        <svg viewBox={`0 0 ${W} ${H}`} className="strat-band-svg" preserveAspectRatio="none" role="img" aria-label="预期区间">
          <path d={area} fill="currentColor" opacity={0.12} />
          <line x1={0} x2={W} y1={y(0)} y2={y(0)} stroke="currentColor" opacity={0.25} strokeDasharray="3 3" />
          <path d={mid} fill="none" stroke="currentColor" opacity={0.4} strokeWidth={1} />
          {paper ? <path d={paper} fill="none" stroke="#2f80ed" strokeWidth={2} /> : null}
        </svg>
      ) : null}
      <div className="muted strat-risk-note">
        {b.note} 区间 {b.name} v{b.version} · seed {b.registered?.seed} · block {b.registered?.block} · {b.registered?.n_paths} 条路径 · 来源 {b.sourceSha}
        {b.error ? ` · ${b.error}` : ""}
      </div>
    </div>
  );
}

function VersionLine({ v }: { v: RunVersion }) {
  const l = v.lastRun;
  return (
    <div className="muted strat-version">
      本次运行版本：commit {short(l.git_commit)} · 参数 {short(l.params_sha)} · 成本模型 {short(l.cost_model_sha)}
      {v.unversionedRuns ? ` · 早期 ${v.unversionedRuns} 天无版本记录` : ""}
      {v.changedSinceLastRun ? " · ⚠ 当前参数与上次运行不同" : ""}
      {l.git_commit && v.current.git_commit && l.git_commit !== v.current.git_commit ? ` · 当前代码 ${short(v.current.git_commit)}` : ""}
    </div>
  );
}
