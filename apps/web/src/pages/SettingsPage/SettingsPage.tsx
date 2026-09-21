import { useEffect, useState } from "react";
import { marketProvider } from "@/providers/HttpWsProvider";
import { useDataSource } from "@/hooks/useDataSource";
import { useStrategyConfig } from "@/hooks/useStrategyConfig";
import { AutoPaperToggle } from "@/components/layout/AutoPaperToggle";
import {
  readShowMonteCarlo,
  readShowPaperStats,
  writeShowMonteCarlo,
  writeShowPaperStats,
} from "@/components/market/PaperStatsPanel";
import { DATA_SOURCES, VENUE } from "@/venue";
import type { DataSource } from "@/venue";

export function SettingsPage() {
  const [provider, setProvider] = useState<string>("…");
  const [mode, setMode] = useState<string>("…");
  const [status, setStatus] = useState<string>("…");
  const [tradingState, setTradingState] = useState<string>("…");
  const [venue, setVenue] = useState<string>("…");
  const [marketOpts, setMarketOpts] = useState<string[]>([]);
  const [discovery, setDiscovery] = useState<string>("…");
  const [portalKey, setPortalKey] = useState<boolean | null>(null);
  const [err, setErr] = useState<string>("");
  const { dataSource, setDataSource } = useDataSource();
  const { autoPaperOrders, setAutoPaperOrders, tradingState: stratState } = useStrategyConfig();
  const [showPaperStats, setShowPaperStats] = useState(true);
  const [showMc, setShowMc] = useState(false);

  useEffect(() => {
    setShowPaperStats(readShowPaperStats());
    setShowMc(readShowMonteCarlo());
  }, []);

  useEffect(() => {
    marketProvider
      .getHealth()
      .then((h) => {
        setProvider(h.provider);
        setMode(h.mode);
        setStatus(h.status);
        setTradingState(h.trading_state ?? "active");
        setVenue(h.venue ?? (h.provider === "pumpfun_paper" ? VENUE : "mock"));
        setMarketOpts(h.marketProviderOptions ?? ["mock", "pumpfun_paper"]);
        setDiscovery(h.discovery ?? "off");
        setPortalKey(Boolean(h.portal_key_configured));
      })
      .catch((e: Error) => setErr(e.message));
  }, []);

  return (
    <div className="shell-page">
      <h1>设置 / Settings</h1>
      <p className="muted">
        纸面默认；无实盘密钥、无钱包。行情 <code>DATA_PROVIDER=mock | pumpfun_paper</code>
        ；下单走 <code>PaperBroker</code>（<code>dataSource=mock | paper | pumpfun_paper</code>）。
        {provider === "pumpfun_paper" ? ` venue=${VENUE}，仅纸面曲线模拟。` : null}
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
          <code>paper</code> / <code>pumpfun_paper</code> 时 Overlay 只订 PaperBroker Fill；告警条听
          reject/risk；拒单不画成交点。Mock 信号叠加始终保留。下单不离开 PaperBroker。
        </p>
      </section>

      <section className="settings-section">
        <h2>Discovery · PUMPFUN_DISCOVERY</h2>
        <p className="muted">
          只读新币发现（进程环境变量）。<code>pumpportal</code> / <code>logs</code> / <code>off</code>
          。无 <code>PUMPFUN_PORTAL_API_KEY</code> 时默认 off；有 key 默认 pumpportal。Key 仅 env，界面不展示。
          发现写入自选并推 WS <code>new_token</code>，<strong>不</strong>下单、无 sniper。
        </p>
        <p className="muted">
          当前 <code>PUMPFUN_DISCOVERY={discovery}</code>
          {" · "}
          portal key={portalKey == null ? "…" : portalKey ? "configured" : "absent"}
        </p>
      </section>

      <section className="settings-section">
        <h2>pump-paper-v1 · auto_paper_orders / strategy_autopaper</h2>
        <AutoPaperToggle
          checked={autoPaperOrders}
          onChange={(v) => void setAutoPaperOrders(v).catch(() => undefined)}
          label="自动纸面下单（默认关）"
        />
        <p className="muted">
          关：只发 <code>signal</code> 叠加。开：满足入场才走 pre-order → PaperBroker。无需重启。
          成功概率见行情/策略页面板或 <code>GET /api/v1/strategy/pump-paper-v1/stats</code>（纸面 journal，非承诺）。无钱包。
          观察蒸馏在「观察」页；<code>copy_trade_enabled=false</code>，apply-distill 不自动打开本开关。
        </p>
      </section>

      <section className="settings-section">
        <h2>PaperStats 面板</h2>
        <AutoPaperToggle
          checked={showPaperStats}
          onChange={(v) => {
            setShowPaperStats(v);
            writeShowPaperStats(v);
          }}
          label="show_paper_stats（默认开）"
        />
        <AutoPaperToggle
          checked={showMc}
          onChange={(v) => {
            setShowMc(v);
            writeShowMonteCarlo(v);
          }}
          label="show_monte_carlo（默认关）"
        />
        <p className="muted">
          摘要条胜率/期望/回撤/笔数。蒙特卡洛仅 <code>?mc=1</code>；n&lt;20 显示「样本不足」，不画假分位带。
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
          <code>{stratState || tradingState}</code>
        </dd>
        <dt>auto_paper_orders</dt>
        <dd>
          <code>{String(autoPaperOrders)}</code>
        </dd>
        <dt>strategy_autopaper</dt>
        <dd>
          <code>{String(autoPaperOrders)}</code>
        </dd>
        <dt>PUMPFUN_DISCOVERY</dt>
        <dd>
          <code>{discovery}</code>
        </dd>
        <dt>portal key</dt>
        <dd>
          <code>{portalKey == null ? "…" : portalKey ? "configured" : "absent"}</code>
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
