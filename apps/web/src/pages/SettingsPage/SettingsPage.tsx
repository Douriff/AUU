import { useEffect, useState } from "react";
import { marketProvider } from "@/providers/HttpWsProvider";
import { useDataSource } from "@/hooks/useDataSource";
import type { DataSource } from "@/types/contracts";

export function SettingsPage() {
  const [provider, setProvider] = useState<string>("…");
  const [mode, setMode] = useState<string>("…");
  const [status, setStatus] = useState<string>("…");
  const [tradingState, setTradingState] = useState<string>("…");
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
      })
      .catch((e: Error) => setErr(e.message));
  }, []);

  return (
    <div className="shell-page">
      <h1>设置 / Settings</h1>
      <p className="muted">
        纸面默认；无实盘密钥。行情仍用 Mock；下单路径可切 <code>mock | paper</code>。
      </p>

      <section className="settings-section">
        <h2>Order path · dataSource</h2>
        <div className="data-source-toggle" role="group" aria-label="dataSource">
          {(["mock", "paper"] as DataSource[]).map((opt) => (
            <button
              key={opt}
              type="button"
              className={dataSource === opt ? "active" : ""}
              onClick={() => setDataSource(opt)}
            >
              {opt}
            </button>
          ))}
        </div>
        <p className="muted">
          当前 <code>dataSource={dataSource}</code>。paper 时 Overlay 订 fill；告警条听
          reject/risk；拒单不画成交点。Mock 信号叠加始终保留。
        </p>
      </section>

      <dl className="settings-dl">
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
