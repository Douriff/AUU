import { useEffect, useState } from "react";
import { marketProvider } from "@/providers/HttpWsProvider";
import type { ReconSummary } from "@/types/mainstream";

/** Cross-source daily close reconciliation (read-only; never switches the strategy's data source). */

const POLL_MS = 300_000;
const SH = new Intl.DateTimeFormat("zh-CN", { timeZone: "Asia/Shanghai", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false });
const pct = (v: number | null | undefined, d = 3) => (v == null || !Number.isFinite(v) ? "—" : `${v.toFixed(d)}%`);
const px = (v: number | null | undefined) => (v == null || !Number.isFinite(v) ? "—" : String(+v.toPrecision(6)));
const STATUS: Record<string, string> = {
  ok: "正常",
  partial: "部分完成",
  error: "对照源不可用",
  deviation: "偏差超阈值",
  missing_primary: "策略源缺数据",
  missing_secondary: "对照源缺数据",
};

export function ReconPanel() {
  const [d, setD] = useState<ReconSummary | null>(null);
  const [err, setErr] = useState("");
  useEffect(() => {
    let alive = true;
    const load = () =>
      marketProvider
        .getRecon()
        .then((x) => alive && (setD(x), setErr("")))
        .catch((e) => alive && setErr(String(e?.message || e)));
    load();
    const id = window.setInterval(load, POLL_MS);
    return () => {
      alive = false;
      window.clearInterval(id);
    };
  }, []);
  const tone = !d?.lastRunAt ? "" : d.flagged ? "down" : d.status === "ok" ? "up" : "";
  return (
    <section className="console-card strat recon" aria-label="跨源对账">
      <header className="console-card-bar strat-head">
        <div className="console-title">跨源对账 · 日线收盘价</div>
        <span className="console-badge">只读 · 不自动切换数据源</span>
      </header>
      {err ? <p className="console-err">{err}</p> : null}
      <div className="strat-universe">
        <div>
          每天 UTC 收盘后，用策略数据源（{d?.primary || "—"}）的最近 {d?.days ?? 7} 根日线收盘价对照 {d?.secondary || "另一家交易所"} 公开 K
          线；偏差超过阈值（默认 {d ? d.thresholdPct : 0.5}%
          {d && Object.keys(d.thresholdOverrides).length
            ? `，${Object.entries(d.thresholdOverrides).map(([k, v]) => `${k} ${v}%`).join("、")}`
            : ""}
          ）或一边缺数据就告警邮件。
        </div>
      </div>
      <div className="strat-kpis">
        <Stat label="上次对账" value={d?.lastRunAt ? SH.format(new Date(d.lastRunAt)) : "尚未运行"} sub={d?.status ? STATUS[d.status] || d.status : ""} />
        <Stat label="核对 / 异常" value={d ? `${d.checked} / ${d.flagged}` : "—"} tone={tone} />
        <Stat label="最大偏差" value={pct(d?.maxDevPct)} sub={d?.maxCoin || ""} />
        <Stat label="对照源错误" value={d?.error ? "有" : "无"} sub={d?.error ? d.error.slice(0, 60) : ""} />
      </div>
      <div className="strat-tables single">
        <div className="recon-scroll">
          <h4>各币最新一天（{d?.coins[0]?.latestDay || "—"} UTC）</h4>
          <table className="num">
            <thead>
              <tr><th>币</th><th>{d?.primary || "策略源"}</th><th>{d?.secondary || "对照源"}</th><th>偏差</th><th>{d?.days ?? 7} 天最大</th><th>状态</th></tr>
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
                    <td className={c.flagged ? "down" : ""}>{c.flagged ? `${STATUS[c.status] || c.status} ×${c.flagged}` : "正常"}</td>
                  </tr>
                ))
              ) : (
                <tr><td colSpan={6} className="muted">还没有对账结果（每天北京时间 08:20 后运行）</td></tr>
              )}
            </tbody>
          </table>
        </div>
      </div>
      {d?.flags.length ? (
        <div className="strat-universe">
          <b>异常明细</b>
          {d.flags.slice(0, 12).map((f) => (
            <div key={`${f.coin}-${f.day}`} className="down">
              {f.day} {f.coin} · {STATUS[f.status] || f.status} · {px(f.c1)} / {px(f.c2)} · {pct(f.dev)}（阈值 {f.threshold}%）
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
