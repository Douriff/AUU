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
import type { LiveStatus } from "@/types/contracts";
import { AccountSection } from "@/pages/SettingsPage/AccountSection";
import { WalletSection } from "@/pages/SettingsPage/WalletSection";
import { useLegacyMode } from "@/hooks/useLegacyMode";
import type { MainstreamFreshness } from "@/types/mainstream";
import { Link } from "react-router-dom";
import { ColorToggle } from "@/components/ui/ColorToggle";
import { ModeBadge } from "@/components/ui/Brand";
import { Sk } from "@/components/ui/Skeleton";
import { useTranslation } from "react-i18next";
import { errText } from "@/i18n/errors";
import { LangSwitch } from "@/components/ui/LangSwitch";

export function SettingsPage() {
  const legacy = useLegacyMode();
  if (legacy === null) return null;
  return legacy ? <LegacySettingsPage /> : <MainstreamSettingsPage />;
}

/** Mainstream mode: account + read-only data/live status (pump wallet/provider/discovery are legacy). */
function MainstreamSettingsPage() {
  const { t } = useTranslation();
  const [md, setMd] = useState<MainstreamFreshness | null>(null);
  const [live, setLive] = useState<LiveStatus | null>(null);
  const [err, setErr] = useState("");
  useEffect(() => {
    marketProvider
      .getHealth()
      .then((h) => setMd(h.mainstream ?? null))
      .catch((e: unknown) => setErr(errText(e, "common.loadFailed")));
    marketProvider
      .getLiveStatus()
      .then((st) => setLive(st))
      .catch(() => undefined);
  }, []);
  const blocked = md ? Object.keys(md.blocked || {}) : [];
  return (
    <div className="shell-page settings-page pro-page">
      <header className="pro-head">
        <h1>{t("shell.settings")}</h1>
        <p>{t("settings.sub")}</p>
      </header>
      <nav className="set-links" aria-label={t("settings.more")}>
        <Link to="/majors">{t("nav.majors")} ›</Link>
        <Link to="/news">{t("news.title")} ›</Link>
        <Link to="/leaderboard">{t("nav.leaderboard")} ›</Link>
      </nav>
      <section className="settings-section">
        <h2>{t("settings.display")}</h2>
        <div className="set-row set-row-lang">
          <div>
            <b>{t("lang.label")} · Language</b>
            <p className="muted">{t("settings.langHint")}</p>
          </div>
          <LangSwitch />
        </div>
        <div className="set-row">
          <div>
            <b>{t("color.label")}</b>
            <p className="muted">{t("settings.colorHint")}</p>
          </div>
          <ColorToggle />
        </div>
      </section>
      <AccountSection />
      <section className="settings-section">
        <h2>{t("settings.modeData")}</h2>
        {err ? <div className="pro-alert">{err}</div> : null}
        <dl className="set-dl">
          <div>
            <dt>{t("settings.mode")}</dt>
            <dd>
              <ModeBadge liveOff={!live?.liveEnabled} />
              <span className="muted">{t("settings.modeNote")}</span>
            </dd>
          </div>
          <div>
            <dt>{t("settings.source")}</dt>
            <dd>
              {md ? (
                <>
                  <b>{(md.exchange || "—").toUpperCase()}</b>
                  <span className="muted">
                    {t("settings.fallback")} {md.exchanges?.map((x) => x.toUpperCase()).join(" → ") || "—"}
                    {blocked.length ? ` · ${t("settings.unavailable")} ${blocked.join(", ").toUpperCase()}` : ""}
                  </span>
                </>
              ) : (
                <Sk w={180} h={12} />
              )}
            </dd>
          </div>
          <div>
            <dt>{t("settings.dataState")}</dt>
            <dd>{md ? <span className={md.stale ? "warn" : "up"}>{md.stale ? t("shell.stale") : t("shell.fresh")}</span> : <Sk w={80} h={12} />}</dd>
          </div>
          <div>
            <dt>{t("ms.col.coin")}</dt>
            <dd className="muted">{md ? t("settings.coins", { n: md.symbols?.length ?? 0, list: md.symbols?.join(" ") }) : <Sk w={240} h={12} />}</dd>
          </div>
        </dl>
      </section>
    </div>
  );
}

function LegacySettingsPage() {
  const [provider, setProvider] = useState<string>("…");
  const [mode, setMode] = useState<string>("…");
  const [status, setStatus] = useState<string>("…");
  const [tradingState, setTradingState] = useState<string>("…");
  const [venue, setVenue] = useState<string>("…");
  const [marketOpts, setMarketOpts] = useState<string[]>([]);
  const [marketData, setMarketData] = useState<string>("");
  const [marketDataLabel, setMarketDataLabel] = useState<string>("");
  const [discovery, setDiscovery] = useState<string>("…");
  const [discoveryActive, setDiscoveryActive] = useState<string>("…");
  const [discoveryReason, setDiscoveryReason] = useState<string>("");
  const [portalKey, setPortalKey] = useState<boolean | null>(null);
  const [live, setLive] = useState<LiveStatus | null>(null);
  const [liveMsg, setLiveMsg] = useState("");
  const [confirmOpen, setConfirmOpen] = useState(false);
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
        setMarketOpts(h.marketProviderOptions ?? ["mock", "pumpfun_paper", "pumpfun_live_paper"]);
        setMarketData(h.marketData ?? "");
        setMarketDataLabel(h.marketDataLabel ?? "");
        setDiscovery(h.discovery ?? "off");
        setDiscoveryActive(h.discoveryActive ?? h.discovery ?? "off");
        setDiscoveryReason(h.discoveryReason ?? "");
        setPortalKey(Boolean(h.portal_key_configured));
      })
      .catch((e: Error) => setErr(e.message));
    marketProvider
      .getLiveStatus()
      .then((st) => setLive(st))
      .catch(() => undefined);
  }, []);

  const mounted = live?.keypairMounted === true || live?.keypairConfigured === true;
  const pubkey = live?.pubkey || "";
  const liveEnabled = Boolean(live?.liveEnabled);

  const applyLive = (st: LiveStatus) => {
    setLive(st);
  };

  const requestEnable = () => {
    setLiveMsg("");
    if (!mounted) {
      setLiveMsg("cannot enable: keypair mounted = no");
      return;
    }
    setConfirmOpen(true);
  };

  const cancelConfirm = () => {
    setConfirmOpen(false);
    setLiveMsg("liveEnabled stays off (confirm cancelled)");
  };

  const confirmEnable = () => {
    setConfirmOpen(false);
    setLiveMsg("");
    void marketProvider
      .putLiveEnabled(true, true)
      .then((st) => {
        applyLive(st);
        setLiveMsg(
          st.liveArmed && st.liveEnabled
            ? "checklist updated; send gate stays closed (zero chain txs)"
            : "liveEnabled requested; LIVE_DISABLED until keypair + confirm + limits"
        );
      })
      .catch((e: Error) => setLiveMsg(e.message));
  };

  const disableLive = () => {
    setLiveMsg("");
    setConfirmOpen(false);
    void marketProvider
      .putLiveEnabled(false, false)
      .then((st) => {
        applyLive(st);
        setLiveMsg("liveEnabled=false");
      })
      .catch((e: Error) => setLiveMsg(e.message));
  };

  return (
    <div className="shell-page">
      <h1>设置 / Settings</h1>
      <p className="muted">
        纸面默认；实盘适配器默认关闭。行情{" "}
        <code>DATA_PROVIDER=mock | pumpfun_paper | pumpfun_live_paper</code>
        ；下单走 <code>PaperBroker</code>（<code>dataSource=mock | paper | pumpfun_paper</code>）。
        {provider === "pumpfun_paper" ? ` venue=${VENUE}，合成行情。` : null}
        {provider === "pumpfun_live_paper" ? ` venue=${VENUE}，真实链上纸面。` : null}
        {" "}
        Live adapter is scaffolded but <strong>liveEnabled defaults off</strong> (no chain submit).
      </p>

      <AccountSection />
      <WalletSection />

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
          {marketData ? (
            <>
              {" "}
              · 行情源=<code>{marketData}</code>
              {marketDataLabel ? `（${marketDataLabel}）` : null}
            </>
          ) : null}
          {venue ? (
            <>
              {" "}
              · venue=<code>{venue}</code>
            </>
          ) : null}
          。<code>pumpfun_paper</code> 为合成曲线（不进 Go）。<code>pumpfun_live_paper</code>{" "}
          只用真实成交与储备，不发链上交易。
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
          {discoveryActive && discoveryActive !== discovery ? (
            <>
              {" · "}
              active=<code>{discoveryActive}</code>
            </>
          ) : null}
          {discoveryReason ? (
            <>
              {" · "}
              reason=<code>{discoveryReason}</code>
            </>
          ) : null}
          {" · "}
          portal key={portalKey == null ? "…" : portalKey ? "configured" : "absent"}
          。Portal HTTP 400 = 畸形/拼接 key；403 = 无效/过期/封禁或 IP 禁（同一时间一条 WS）。
        </p>
      </section>

      <section className="settings-section live-section">
        <h2>Live adapter · Pump.fun local signer (dark)</h2>
        <p className="muted">
          liveEnabled=<code>{String(live?.liveEnabled ?? false)}</code>
          {" · "}
          liveConfirmed=<code>{String(live?.liveConfirmed ?? false)}</code>
          {" · "}
          sendEnabled=<code>{String(live?.sendEnabled ?? false)}</code>
          . Caps are locked (read-only). This UI never accepts a secret blob.
        </p>
        <p className="live-status-row">
          <span className="mode-badge live-off">LIVE DISABLED</span>
          <span className="mode-badge live-off">{liveEnabled ? "ENABLED (SEND OFF)" : "liveEnabled OFF"}</span>
          {(live?.reasons ?? ["LIVE_DISABLED"]).map((tag) => (
            <span key={tag} className="tag">
              {tag}
            </span>
          ))}
        </p>
        <dl className="settings-dl live-caps">
          <dt>max_notional_sol</dt>
          <dd>
            <code>{live?.limits.max_notional_sol ?? 1}</code> SOL / order (locked)
          </dd>
          <dt>max_day_loss_pct</dt>
          <dd>
            <code>{live?.limits.max_day_loss_pct ?? 0.045}</code> (4.5%) (locked)
          </dd>
          <dt>max_open_mints</dt>
          <dd>
            <code>{live?.limits.max_open_mints ?? 10}</code> concurrent (locked)
          </dd>
          <dt>keypair</dt>
          <dd>
            mounted: <code>{String(mounted)}</code>
            {pubkey ? (
              <>
                {" "}
                · pubkey <code>{pubkey}</code>
              </>
            ) : null}
          </dd>
        </dl>
        <div className="paper-actions">
          {liveEnabled ? (
            <button type="button" className="ghost" onClick={disableLive}>
              Turn liveEnabled off
            </button>
          ) : (
            <button type="button" className="ghost" disabled={!mounted} onClick={requestEnable}>
              Enable live…
            </button>
          )}
        </div>
        <p className="muted">
          liveEnabled defaults off. Mount a local JSON array of 64 ints at{" "}
          <code>secrets/live-keypair.json</code> (gitignored; Phantom base58 converted locally) or
          set <code>AUU_SOLANA_KEYPAIR_PATH</code>. Health shows mounted + pubkey only (
          <code>8fs58PRKhWy8jVkm7Ro6umY2jxbjtoY33LjyUb6YakFi</code>). Enabling requires a
          secondary confirm dialog and the three locked caps. This runtime still sends zero
          chain txs. This UI never asks for a secret.
        </p>
        {liveMsg ? <p className="muted">{liveMsg}</p> : null}
      </section>

      {confirmOpen ? (
        <div className="live-confirm-overlay" role="dialog" aria-modal="true" aria-labelledby="live-confirm-title">
          <div className="live-confirm-dialog">
            <h2 id="live-confirm-title">Confirm liveEnabled</h2>
            <p>
              Secondary confirm required. Keypair mounted: <code>{String(mounted)}</code>
              {pubkey ? (
                <>
                  {" "}
                  pubkey <code>{pubkey}</code>
                </>
              ) : null}
              . Caps stay locked at 1 SOL / 4.5% / 10 mints. Send gate stays closed — zero chain
              transactions in this runtime.
            </p>
            <div className="paper-actions">
              <button type="button" className="ghost" onClick={cancelConfirm}>
                Cancel
              </button>
              <button type="button" className="buy" onClick={confirmEnable}>
                Confirm liveEnabled
              </button>
            </div>
          </div>
        </div>
      ) : null}

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
          观察蒸馏在「观察」页；应用蒸馏<strong>不会</strong>打开本开关或实盘。
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
        <dt>discoveryActive</dt>
        <dd>
          <code>{discoveryActive}</code>
        </dd>
        <dt>discoveryReason</dt>
        <dd>
          <code>{discoveryReason || "—"}</code>
        </dd>
        <dt>portal key</dt>
        <dd>
          <code>{portalKey == null ? "…" : portalKey ? "configured" : "absent"}</code>
        </dd>
        <dt>liveEnabled</dt>
        <dd>
          <code>{String(live?.liveEnabled ?? false)}</code>
        </dd>
        <dt>liveConfirmed</dt>
        <dd>
          <code>{String(live?.liveConfirmed ?? false)}</code>
        </dd>
        <dt>liveDisabled</dt>
        <dd>
          <code>{String(live?.liveDisabled ?? true)}</code>
        </dd>
        <dt>keypair mounted</dt>
        <dd>
          <code>{String(mounted)}</code>
        </dd>
        <dt>pubkey</dt>
        <dd>
          <code>{pubkey || "—"}</code>
        </dd>
        <dt>live caps</dt>
        <dd>
          <code>
            {live
              ? `${live.limits.max_notional_sol} SOL / ${live.limits.max_day_loss_pct} / ${live.limits.max_open_mints}`
              : "1 SOL / 0.045 / 10"}
          </code>
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
