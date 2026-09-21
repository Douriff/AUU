import { useEffect, useState } from "react";
import { marketProvider } from "@/providers/HttpWsProvider";
import { useDataSource } from "@/hooks/useDataSource";
import { DATA_SOURCES, VENUE } from "@/venue";
import type { DataSource } from "@/venue";

export function SettingsPage() {
  const [provider, setProvider] = useState<string>("…");
  const [mode, setMode] = useState<string>("…");
  const [status, setStatus] = useState<string>("…");
  const [tradingState, setTradingState] = useState<string>("…");
  const [venue, setVenue] = useState<string>(VENUE);
  const [err, setErr] = useState<string>("");
  const { dataSource, setDataSource } = useDataSource();

  useEffect(() => {
    marketProvider
      .getHealth()
      .then((h) => {
        setProvider(h.provider);
        setMode(h.mode);
        setStatus(h.status);
        setTradingState(h.trading_state ?? "active");
        if (h.venue) setVenue(h.venue);
      })
      .catch((e: Error) => setErr(e.message));
  }, []);

  return (
    <div className="shell-page">
      <h1>设置 / Settings</h1>
      <p className="muted">
        主场 <code>venue={venue}</code>（Solana bonding curve）。纸面 / mock 优先；无钱包私钥、无自动买币
        sniper。
      </p>

      <section className="settings-section">
        <h2>Order path · dataSource</h2>
        <div className="data-source-toggle" role="group" aria-label="dataSource">
          {DATA_SOURCES.map((opt) => (
            <button
              key={opt}
              type="button"
              className={dataSource === opt ? "active" : ""}
              onClick={() => setDataSource(opt as DataSource)}
            >
              {opt}
            </button>
          ))}
        </div>
        <p className="muted">
          当前 <code>dataSource={dataSource}</code>。
          <code>paper</code> / <code>pumpfun_paper</code> 时 Overlay 只订 PaperBroker Fill；
          <code>pumpfun_paper</code> 走 mock 曲线仿真（virtual SOL/token reserves、progress、毕业/迁移）。拒单不画成交点。
        </p>
      </section>

      <dl className="settings-dl">
        <dt>venue</dt>
        <dd>
          <code>{venue}</code>
        </dd>
        <dt>DATA_PROVIDER</dt>
        <dd>
          <code>{provider}</code>
        </dd>
        <dt>MODE</dt>
        <dd>
          <code>{mode}</code>
        </dd>
        <dt>trading_state</dt>
        <dd>
          <code>{tradingState}</code>
        </dd>
        <dt>API health</dt>
        <dd>
          <code>{status}</code>
        </dd>
        <dt>VITE_API_BASE</dt>
        <dd>
          <code>{import.meta.env.VITE_API_BASE || "(same-origin / vite proxy)"}</code>
        </dd>
      </dl>
      {err ? <p className="error">健康检查失败：{err}</p> : null}
    </div>
  );
}
