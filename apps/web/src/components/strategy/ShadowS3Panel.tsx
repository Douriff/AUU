import { useEffect, useState } from "react";
import { marketProvider } from "@/providers/HttpWsProvider";
import type { ShadowS3Summary } from "@/types/mainstream";

/** Shadow record of the S3 hypothesis (long after extreme negative funding). No capital; not evidence. */

const POLL_MS = 120_000;
const SH = new Intl.DateTimeFormat("zh-CN", { timeZone: "Asia/Shanghai", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false });
const bp = (v: number | null | undefined) => (v == null || !Number.isFinite(v) ? "—" : `${v > 0 ? "+" : ""}${v.toFixed(1)} bp`);
const pct = (v: number | null | undefined, d = 2) =>
  v == null || !Number.isFinite(v) ? "—" : `${v > 0 ? "+" : ""}${(v * 100).toFixed(d)}%`;
const STATUS: Record<string, string> = { pending: "待入场", open: "持有中", closed: "已平", void: "作废" };

export function ShadowS3Panel() {
  const [d, setD] = useState<ShadowS3Summary | null>(null);
  const [err, setErr] = useState("");
  useEffect(() => {
    let alive = true;
    const load = () =>
      marketProvider
        .getShadowS3()
        .then((x) => alive && (setD(x), setErr("")))
        .catch((e) => alive && setErr(String(e?.message || e)));
    load();
    const id = window.setInterval(load, POLL_MS);
    return () => {
      alive = false;
      window.clearInterval(id);
    };
  }, []);
  const ev = d?.evaluations[d.evaluations.length - 1];
  return (
    <section className="console-card strat shadow" aria-label="影子假设 S3">
      <header className="console-card-bar strat-head">
        <div className="console-title">影子假设 · S3 负资金费后做多</div>
        <span className="console-badge shadow-badge">{d?.label || "影子假设，非证据，需 ≥100 笔新交易再评估"}</span>
        <span className="console-badge">不占资金 · 独立账本</span>
      </header>
      {err ? <p className="console-err">{err}</p> : null}
      <div className="strat-universe">
        <div>
          <b>规则（已写死{d && !d.ruleFrozen ? "，⚠ 规则哈希与登记时不一致" : ""}）</b>：资金费结算折算 8h 费率 ≤ −0.1% 时，在该小时永续 1h
          收盘做多，持有 72h 后收盘平仓；同一币一次一笔；扣 taker 0.05%×2 + 滑点和期间资金费。
        </div>
        <div>
          登记于 {d ? SH.format(new Date(d.registeredAt)) : "—"}（只统计此后的新信号）· 数据：{d?.source || "—"} · 规则 {d?.ruleHash || ""}
        </div>
        {(d?.notes || []).map((n) => <div key={n} className="muted">{n}</div>)}
      </div>
      <div className="strat-kpis">
        <Stat label="评估进度" value={d ? `${d.progress} / ${d.evalAt} 笔` : "—"} sub="满 100 笔自动按报告口径评估" />
        <Stat label="持有中 / 待入场" value={d ? `${d.counts.open} / ${d.counts.pending}` : "—"} />
        <Stat label="作废" value={d ? String(d.counts.void) : "—"} sub="缺少 bar 价格" />
        <Stat label="累计均净（记录）" value={bp(d?.running?.mean_net_bps)} sub={d?.runningNote} />
        <Stat label="胜率（记录）" value={d?.running ? `${(d.running.win * 100).toFixed(0)}%` : "—"} />
        <Stat
          label="评估结论"
          value={ev ? (ev.verdict === "candidate" ? "候选（仍需你确认）" : "不通过") : "未到 100 笔"}
          sub={ev ? `均净 ${bp(ev.mean_net_bps)} · CI [${ev.ci_bps.map((x) => x.toFixed(0)).join(", ")}] · 去最好3笔 ${bp(ev.ex_best3_bps)}` : ""}
        />
      </div>
      <div className="strat-tables single">
        <div>
          <h4>假设交易（最近 12 笔）</h4>
          <table className="num">
            <thead>
              <tr><th>币</th><th>信号（北京）</th><th>8h 费率</th><th>状态</th><th>入场</th><th>出场</th><th>净收益</th></tr>
            </thead>
            <tbody>
              {d?.trades.length ? (
                d.trades.slice(0, 12).map((t) => (
                  <tr key={t.id}>
                    <td>{t.coin}</td>
                    <td>{SH.format(new Date(t.signal_ts))}</td>
                    <td>{pct(t.f8, 3)}</td>
                    <td>{STATUS[t.status] || t.status}</td>
                    <td>{t.entry_px ?? "—"}</td>
                    <td>{t.exit_px ?? "—"}</td>
                    <td className={t.net == null ? "" : t.net > 0 ? "up" : "down"}>{pct(t.net)}</td>
                  </tr>
                ))
              ) : (
                <tr><td colSpan={7} className="muted">暂无信号（费率 ≤ −0.1%/8h 很少见，报告留出期约 1 笔/月）</td></tr>
              )}
            </tbody>
          </table>
        </div>
      </div>
    </section>
  );
}

function Stat({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <div className="console-stat">
      <span>{label}</span>
      <strong>{value}</strong>
      {sub ? <small className="muted">{sub}</small> : null}
    </div>
  );
}
