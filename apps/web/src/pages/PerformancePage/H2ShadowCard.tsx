import { useEffect, useState } from "react";
import { marketProvider } from "@/providers/HttpWsProvider";
import type { ShadowH2Summary } from "@/types/mainstream";
import { Sk } from "@/components/ui/Skeleton";

/** H2 forward shadow (pre-registered, frozen params, no capital). Read-only; login enforced by AppShell + API 401. */

const DAY_MS = 86_400_000;
const pct = (v: number | null | undefined, d = 2) =>
  v == null || !Number.isFinite(v) ? "—" : `${v > 0 ? "+" : ""}${(v * 100).toFixed(d)}%`;
const usd = (v: number | null | undefined) =>
  v == null || !Number.isFinite(v) ? "—" : `${v > 0 ? "+" : ""}${v.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
const tn = (v: number | null | undefined) => (v == null || !Number.isFinite(v) || v === 0 ? "" : v > 0 ? "up" : "down");

/** "2026-10-04" -> "10/4" */
export function shortDay(day: string | null | undefined): string {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(day ?? "");
  return m ? `${Number(m[2])}/${Number(m[3])}` : day ?? "—";
}

/** inception + n days, as YYYY-MM-DD (UTC days, same as the ledger). */
export function addDays(day: string | null | undefined, n: number): string {
  const t = Date.parse(`${day}T00:00:00Z`);
  return Number.isFinite(t) ? new Date(t + n * DAY_MS).toISOString().slice(0, 10) : "—";
}

export function H2ShadowCard() {
  const [h2, setH2] = useState<ShadowH2Summary | null>(null);
  const [err, setErr] = useState("");

  useEffect(() => {
    let alive = true;
    marketProvider
      .getShadowH2()
      .then((d) => alive && (setH2(d), setErr("")))
      .catch((e) => alive && setErr(String(e?.message || e)));
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
        <h2>H2 影子盘</h2>
        <span className="tag h2-tag">只记录 · 不下单 · 不投钱</span>
        {h2?.paramsSha256 ? (
          <span className={`h2-frozen ${h2.paramsFrozen === false || h2.refused ? "down" : "dim"}`} title={h2.paramsSha256}>
            {h2.paramsFrozen === false || h2.refused ? "参数哈希不一致" : "参数已冻结"} · {h2.paramsSha256.slice(0, 12)}
          </span>
        ) : null}
      </div>

      {err ? <div className="pro-alert">H2 影子盘读取失败：{err}</div> : null}
      {!h2 && !err ? <Sk h={88} r={6} className="sk-block" /> : null}

      {h2?.refused ? <div className="pro-alert">参数与登记不一致，程序已拒绝运行：{h2.refused}</div> : null}
      {h2?.aborted ? <div className="pro-alert">已触发中止条件（判失败）：{h2.aborted}</div> : null}
      {h2?.paused ? <div className="h2-warn">记录落后 {h2.lagDays} 天，已暂停计入</div> : null}

      {h2 && !last ? (
        <div className="h2-empty">
          <b>已登记，首个记录日 {shortDay(inc)} 收盘</b>
          <span className="muted">北京时间次日 08:15 后写入第一条记录，之后每天更新一次；每日 08:30 的摘要邮件里也有这一行。</span>
          {h2.waiting ? <span className="muted">当前状态：{h2.waiting}</span> : null}
        </div>
      ) : null}

      {last && cum ? (
        <>
          <div className="perf-kpis h2-kpis">
            {(
              [
                [`当日收益 · ${shortDay(last.dayStr)}`, pct(last.ret, 3), tn(last.ret)],
                ["累计收益", pct(cum.ret, 3), tn(cum.ret)],
                ["超额 T-bill", pct(cum.excess, 3), tn(cum.excess)],
                ["同期 T-bill", pct(cum.tbill, 3), ""],
                ["影子净值", last.nav.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 }), ""],
                [`最大回撤（${pct(ddLimit, 0)} 判失败）`, pct(cum.maxDrawdown), tn(cum.maxDrawdown)],
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
                  <th>三腿盈亏（USDT）</th>
                  <th>当日</th>
                  <th>累计</th>
                </tr>
              </thead>
              <tbody>
                {(
                  [
                    [`趋势（权重 ${(last.w_trend * 100).toFixed(1)}%）`, last.pnl_trend, cum.pnlTrend],
                    [`资金费套利（权重 ${(last.w_carry * 100).toFixed(1)}%）`, last.pnl_carry, cum.pnlCarry],
                    ["闲置资金生息", last.pnl_idle, cum.pnlIdle],
                    ["调仓成本", 0 - last.cost_sleeve, 0 - cum.costSleeve],
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
            <span>进度</span>
            <b className="num">
              {n}/{days} 天
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
          过关标准：满 {days} 天（{end}）后只评估一次——扣除国债收益后的年化超额收益，95% 置信区间下限 &gt; 0 才算通过，中途不提前宣布。
          直接判失败：从高点回撤超过 {pct(Math.abs(ddLimit), 0).replace("+", "")}，或套利腿爆仓。未满 {days} 天的数字只是记录，不是证据。
        </p>
      ) : null}
    </section>
  );
}
