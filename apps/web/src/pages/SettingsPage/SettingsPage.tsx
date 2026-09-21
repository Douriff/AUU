import { useEffect, useState } from "react";
import { marketProvider } from "@/providers/HttpWsProvider";
import { useDataSource } from "@/hooks/useDataSource";
import type { DataSource } from "@/types/contracts";

export function SettingsPage() {
  const [provider, setProvider] = useState<string>("…");
  const [mode, setMode] = useState<string>("…");
  const [status, setStatus] = useState<string>("…");
  const [tradingState, setTradingState] = useState<string>("…");
  const [venue, setVenue] = useState<string>("…");
  const [marketOpts, setMarketOpts] = useState<string[]>([]);
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
        setVenue(h.venue ?? (h.provider === "pumpfun_paper" ? "Pump.fun" : "mock"));
        setMarketOpts(h.marketProviderOptions ?? ["mock", "pumpfun_paper"]);
      })
      .catch((e: Error) => setErr(e.message));
  }, []);

  return (
    <div className="shell-page">
      <h1>设置 / Settings</h1>
      <p className="muted">
        纸面默认；无实盘密钥、无钱包。行情 <code>DATA_PROVIDER=mock | pumpfun_paper</code>
        ；下单仍走 <code>PaperBroker</code>（<code>dataSource=mock | paper</code>）。
        {provider === "pumpfun_paper" ? " venue=Pump.fun，仅纸面曲线模拟。" : null}
      </p>

      <section className="settings-section">
        <h2>Market · DATA_PROVIDER</h2>
        <p className="muted">
          只读（进程环境变量）。可选{" "}
          {(marketOpts.length ? marketOpts : ["mock", "pumpfun_paper"]).map((opt, i) => (
            <span key={opt}>
              {i ? " · " : null}
              <code className={opt === provider ? "hl" : undefined}>{opt}</code>
            </span>
          ))}
        </p>
        <p className="muted">
          当前 <code>DATA_PROVIDER={provider}</code>
          {venue ? (
            <>
              {" "}
              · venue=<code>{venue}</code>
            </>
          ) : null}
          。<code>pumpfun_paper</code> 为本地 bonding-curve 模拟（watch-mints env），不连钱包。
        </p>
      </section>

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
          reject/risk；拒单不画成交点。Mock 信号叠加始终保留。下单不离开 PaperBroker。
        </p>
      </section>

      <dl className="settings-dl">
        <dt>DATA_PROVIDER</dt>
        <dd>
          <code>{provider}</code>
        </dd>
        <dt>venue</dt>
        <dd>
          <code>{venue}</code>
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
