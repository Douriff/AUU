import { useCallback, useEffect, useState } from "react";
import { marketProvider, type AdminExchange } from "@/providers/HttpWsProvider";
import { errText } from "@/i18n/errors";

/** Admin: exchange connect toggle (default OFF), allowlist, kill switch, caps, audit. */
export function AdminExchangePanel() {
  const [s, setS] = useState<AdminExchange | null>(null);
  const [err, setErr] = useState("");
  const [who, setWho] = useState("");
  const [cap, setCap] = useState("");
  const [defCap, setDefCap] = useState("");
  const [audit, setAudit] = useState<{ ts: number; userId: string; actor: string; action: string; detail: Record<string, unknown> }[]>([]);

  const load = useCallback(() => {
    marketProvider.adminExchange().then(setS).catch((e: unknown) => setErr(errText(e)));
    marketProvider.adminExchangeAudit().then((r) => setAudit(r.items)).catch(() => undefined);
  }, []);
  useEffect(() => load(), [load]);

  const put = (body: Parameters<typeof marketProvider.adminExchangeSet>[0], ask?: string) => {
    if (ask && !window.confirm(ask)) return;
    setErr("");
    marketProvider.adminExchangeSet(body).then((r) => { setS(r); load(); }).catch((e: unknown) => setErr(errText(e)));
  };
  const allow = (user: string, on: boolean) => {
    setErr("");
    const c = Number(cap);
    marketProvider
      .adminExchangeAllow({ user, allow: on, ...(on && c > 0 ? { capUsdt: c } : {}) })
      .then((r) => { setS(r); setWho(""); setCap(""); load(); })
      .catch((e: unknown) => setErr(errText(e)));
  };

  return (
    <div className="shell-page settings-page pro-page">
      <header className="pro-head">
        <h1>交易所接入</h1>
        <p>用户自带 Binance / OKX 现货 API Key。与模拟盘、liveEnabled 完全独立。默认关闭。</p>
      </header>
      {err ? <div className="pro-alert">{err}</div> : null}
      {!s ? <p className="muted">加载中…</p> : (
        <>
          <section className="settings-section">
            <h2>开关</h2>
            <div className="set-row">
              <div><b>功能总开关：{s.enabled ? "开" : "关"}</b><p className="muted">关闭时用户只能看指南、解绑。</p></div>
              <button type="button" className="td-link" onClick={() => put({ enabled: !s.enabled }, s.enabled ? "关闭交易所接入？" : "打开交易所接入？")}>{s.enabled ? "关闭" : "打开"}</button>
            </div>
            <div className="set-row">
              <div><b>仅白名单用户：{s.allowlistOnly ? "是" : "否（所有登录用户）"}</b></div>
              <button type="button" className="td-link" onClick={() => put({ allowlistOnly: !s.allowlistOnly }, s.allowlistOnly ? "对所有登录用户开放？" : "改回仅白名单？")}>{s.allowlistOnly ? "对所有人开放" : "改回仅白名单"}</button>
            </div>
            <div className="set-row">
              <div><b>紧急停止（Kill Switch）：{s.killSwitch ? "已开启 · 禁止所有新单" : "未开启"}</b><p className="muted">开启后所有用户都不能下新单，撤单可用。</p></div>
              <button type="button" className="td-submit" onClick={() => put({ killSwitch: !s.killSwitch }, s.killSwitch ? "解除紧急停止？" : "立即开启紧急停止？")}>{s.killSwitch ? "解除" : "紧急停止"}</button>
            </div>
            <div className="set-row">
              <div><b>默认单笔上限：{s.maxOrderUsdt} USDT</b></div>
              <span>
                <input inputMode="decimal" value={defCap} onChange={(e) => setDefCap(e.target.value)} placeholder="USDT" maxLength={10} style={{ width: 90 }} />{" "}
                <button type="button" className="td-link" disabled={!(Number(defCap) > 0)} onClick={() => put({ maxOrderUsdt: Number(defCap) })}>保存</button>
              </span>
            </div>
            <p className="muted">加密主密钥：{s.cryptoReady ? "已配置" : "未配置（不能绑定）"} · 已绑定：{Object.entries(s.boundCounts).map(([k, v]) => `${k} ${v}`).join("，") || "0"} · 服务器 IP：{s.serverIps.primary} / {s.serverIps.standby}</p>
          </section>
          <section className="settings-section">
            <h2>白名单用户</h2>
            <div className="set-row">
              <input value={who} onChange={(e) => setWho(e.target.value)} placeholder="用户名" maxLength={64} />
              <input inputMode="decimal" value={cap} onChange={(e) => setCap(e.target.value)} placeholder="单笔上限 USDT（可空）" maxLength={10} />
              <button type="button" className="td-link" disabled={!who.trim()} onClick={() => allow(who.trim(), true)}>加入 / 更新</button>
            </div>
            {s.allowlistUsers.length === 0 ? <p className="muted">空</p> : (
              <ul>
                {s.allowlistUsers.map((u) => (
                  <li key={u.id}>{u.name} · 上限 {u.capUsdt ?? s.maxOrderUsdt} USDT <button type="button" className="td-link" onClick={() => allow(u.id, false)}>移除</button></li>
                ))}
              </ul>
            )}
          </section>
          <section className="settings-section">
            <h2>审计日志（最近 100 条）</h2>
            <div className="pro-table-wrap">
              <table className="pro-table">
                <thead><tr><th>时间</th><th>用户</th><th>操作人</th><th>动作</th><th>详情</th></tr></thead>
                <tbody>
                  {audit.map((a, i) => (
                    <tr key={i}><td>{new Date(a.ts).toLocaleString()}</td><td>{a.userId.slice(0, 8)}</td><td>{a.actor.slice(0, 8)}</td><td>{a.action}</td><td><code>{JSON.stringify(a.detail).slice(0, 160)}</code></td></tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>
        </>
      )}
    </div>
  );
}
