import { NavLink, Outlet } from "react-router-dom";
import { useStrategyConfig } from "@/hooks/useStrategyConfig";
import { useEffect, useState, type ReactNode } from "react";
import { marketProvider } from "@/providers/HttpWsProvider";
import { AutoPaperToggle } from "@/components/layout/AutoPaperToggle";

const primary = [
  { to: "/", label: "总览", title: "盘面", end: true, icon: "grid" },
  { to: "/markets", label: "市场", icon: "bars" },
  { to: "/trade", label: "持仓", icon: "bag" },
  { to: "/strategy", label: "策略/影子", icon: "sliders" },
  { to: "/review", label: "复盘", icon: "loop" },
  { to: "/settings", label: "设置", icon: "gear" },
] as const;

const more = [
  { to: "/market", label: "行情" },
  { to: "/watch", label: "观察" },
  { to: "/backtest", label: "回测" },
  { to: "/alerts", label: "告警" },
];

function Icon({ name }: { name: string }) {
  const common = {
    width: 18,
    height: 18,
    viewBox: "0 0 24 24",
    fill: "none",
    stroke: "currentColor",
    strokeWidth: 1.7,
    strokeLinecap: "round" as const,
    strokeLinejoin: "round" as const,
    "aria-hidden": true,
  };
  const paths: Record<string, ReactNode> = {
    grid: (
      <>
        <rect x="3.5" y="3.5" width="7" height="7" rx="1.5" />
        <rect x="13.5" y="3.5" width="7" height="7" rx="1.5" />
        <rect x="3.5" y="13.5" width="7" height="7" rx="1.5" />
        <rect x="13.5" y="13.5" width="7" height="7" rx="1.5" />
      </>
    ),
    bars: (
      <>
        <path d="M4 19V10" />
        <path d="M10 19V5" />
        <path d="M16 19v-7" />
        <path d="M22 19V8" />
      </>
    ),
    bag: (
      <>
        <path d="M6 8h12l-1 12H7L6 8z" />
        <path d="M9 8V7a3 3 0 0 1 6 0v1" />
      </>
    ),
    sliders: (
      <>
        <path d="M4 7h16" />
        <path d="M4 12h16" />
        <path d="M4 17h16" />
        <circle cx="8" cy="7" r="2" fill="#0c0d10" />
        <circle cx="15" cy="12" r="2" fill="#0c0d10" />
        <circle cx="11" cy="17" r="2" fill="#0c0d10" />
      </>
    ),
    loop: (
      <>
        <path d="M4 12a7 7 0 0 1 12-4" />
        <path d="M16 4v4h-4" />
        <path d="M20 12a7 7 0 0 1-12 4" />
        <path d="M8 20v-4h4" />
      </>
    ),
    gear: (
      <>
        <circle cx="12" cy="12" r="3" />
        <path d="M12 3.5v2.2M12 18.3v2.2M3.5 12h2.2M18.3 12h2.2M6 6l1.6 1.6M16.4 16.4 18 18M18 6l-1.6 1.6M7.6 16.4 6 18" />
      </>
    ),
  };
  return <svg {...common}>{paths[name]}</svg>;
}

export function AppShell() {
  const { tradingState, autoPaperOrders, setAutoPaperOrders } = useStrategyConfig();
  const [provider, setProvider] = useState("…");
  const [liveOff, setLiveOff] = useState(true);
  const [collapsed, setCollapsed] = useState(false);

  useEffect(() => {
    marketProvider
      .getHealth()
      .then((h) => {
        setProvider(h.provider);
        setLiveOff(h.liveDisabled !== false || h.liveEnabled === false);
      })
      .catch(() => undefined);
  }, []);

  return (
    <div className={`app-shell${collapsed ? " is-collapsed" : ""}`}>
      <aside className="sidebar" aria-label="主导航">
        <div className="side-brand">
          <span className="side-mark" aria-hidden="true">
            A
          </span>
          <span className="side-word">AUU</span>
        </div>
        <nav className="side-nav">
          {primary.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={"end" in item ? item.end : false}
              title={"title" in item ? item.title : item.label}
              className={({ isActive }) => (isActive ? "is-active" : "")}
            >
              <Icon name={item.icon} />
              <span className="side-label">{item.label}</span>
            </NavLink>
          ))}
        </nav>
        <div className="side-more">
          {more.map((item) => (
            <NavLink key={item.to} to={item.to} className={({ isActive }) => (isActive ? "is-active" : "")}>
              <span className="side-label">{item.label}</span>
            </NavLink>
          ))}
        </div>
        <button
          type="button"
          className="side-collapse"
          onClick={() => setCollapsed((v) => !v)}
          aria-pressed={collapsed}
        >
          {collapsed ? "»" : "«"}
        </button>
      </aside>
      <div className="shell-body">
        <header className="shell-status">
          <span className="trading-state" data-state={tradingState} title="RiskGate trading_state">
            {tradingState}
          </span>
          <span className="muted topbar-provider" title="DATA_PROVIDER">
            {provider}
          </span>
          <AutoPaperToggle
            compact
            label="自动纸面"
            checked={autoPaperOrders}
            onChange={(v) => void setAutoPaperOrders(v).catch(() => undefined)}
          />
          <div className="mode-badge">PAPER · PUMP.FUN</div>
          <div className="mode-badge live-off" title="liveEnabled=false · LIVE_DISABLED">
            {liveOff ? "LIVE OFF" : "LIVE CHECKLIST"}
          </div>
        </header>
        <main className="main">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
