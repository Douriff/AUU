import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { marketProvider } from "@/providers/HttpWsProvider";
import type { PublicStatus } from "@/types/contracts";

/** Login-free system status (coarse only: no positions, prices, versions or internal details). */

const POLL_MS = 60_000;

function pct(v: number | null | undefined, d = 2) {
  return v == null ? "—" : `${(v * 100).toFixed(d)}%`;
}
function bj(ms: number | null | undefined) {
  if (!ms) return "—";
  const t = new Date(ms + 8 * 3_600_000);
  const p = (x: number) => String(x).padStart(2, "0");
  return `${p(t.getUTCMonth() + 1)}-${p(t.getUTCDate())} ${p(t.getUTCHours())}:${p(t.getUTCMinutes())}`;
}
function dur(min: number) {
  return min >= 60 ? `${Math.floor(min / 60)} 小时 ${min % 60} 分` : `${min} 分钟`;
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
        .catch((e: unknown) => setErr(e instanceof Error ? e.message : "无法连接"));
    load();
    const t = window.setInterval(() => !document.hidden && load(), POLL_MS);
    return () => window.clearInterval(t);
  }, []);

  const u = s?.uptime30d;
  const md = s?.marketData;
  const st = s?.strategy;
  return (
    <div className="st-page">
      <header className="st-head">
        <h1>系统状态</h1>
        <span className={`st-badge ${err ? "bad" : s?.healthy ? "ok" : s ? "warn" : ""}`}>
          {err ? "API 无法连接" : !s ? "检查中…" : s.healthy ? "全部正常" : "部分异常"}
        </span>
      </header>
      {err ? <p className="pt-warn">{err}</p> : null}
      <ul className="st-list">
        <Row ok={err ? false : s ? true : null} label="API" value={err ? "离线" : s ? "在线" : "—"} />
        <Row ok={md ? md.fresh : null} label="行情数据" value={md ? (md.fresh ? "新鲜" : `${md.staleSeries} 个序列过期`) : "—"} />
        <Row
          ok={md ? md.exchangesBlocked === 0 || md.exchangesBlocked < md.exchangesTotal : null}
          label="交易所连接"
          value={md ? (md.exchangesBlocked ? `${md.exchangesBlocked}/${md.exchangesTotal} 个被限制（自动切换备用）` : "正常") : "—"}
        />
        <Row
          ok={st ? !st.overdue : null}
          label="上次调仓"
          value={st?.lastRebalanceAt ? `${bj(st.lastRebalanceAt)}（${st.lastRebalanceDay} 日线）${st.overdue ? " · 已超时" : ""}` : "—"}
        />
        <Row ok={s ? s.liveTrading === "locked" : null} label="实盘交易" value={s ? (s.liveTrading === "locked" ? "锁定（只做纸面）" : "未锁定") : "—"} />
      </ul>

      <section className="st-box">
        <div className="st-box-head">
          <b>近 30 天可用率</b>
          <span className="muted">{u?.since ? `自 ${bj(u.since)} 起记录` : "暂无记录"}</span>
        </div>
        <div className="st-kpis num">
          <div><span className="muted">API 在线</span><b>{pct(u?.upPct, 3)}</b></div>
          <div><span className="muted">全部检查通过</span><b>{pct(u?.healthyPct, 3)}</b></div>
        </div>
        <div className="st-bars" role="img" aria-label="每日可用率">
          {(u?.days ?? []).map((d) => {
            const h = d.healthyPct ?? 0;
            const cls = d.measuredMin === 0 ? "" : h >= 0.999 ? "ok" : h >= 0.95 ? "warn" : "bad";
            return <span key={d.day} className={`st-bar ${cls}`} title={`${d.day}  在线 ${pct(d.upPct)} · 正常 ${pct(d.healthyPct)}`} />;
          })}
        </div>
        <p className="muted st-note">
          每分钟记录一次。“在线”= API 在运行；“全部检查通过”= 同时满足：实盘锁定、行情新鲜、调仓未超时（与上线守护 auu-guard 的检查一致）。没有记录的分钟按离线计。
        </p>
        <div className="st-box-head"><b>最近异常</b></div>
        <ul className="st-outages num">
          {(u?.outages ?? []).map((o) => (
            <li key={`${o.kind}-${o.start}`}>
              <span className={o.kind === "down" ? "down" : "warn"}>{o.kind === "down" ? "离线" : "降级"}</span>
              <span>{bj(o.start)}</span>
              <span className="muted">{dur(o.minutes)}</span>
            </li>
          ))}
          {u && u.outages.length === 0 ? <li className="muted">记录期内没有 ≥5 分钟的异常</li> : null}
        </ul>
      </section>
      <p className="muted st-note">
        时间为北京时间 · 每分钟自动刷新 · <Link to="/login">登录</Link>
      </p>
    </div>
  );
}
