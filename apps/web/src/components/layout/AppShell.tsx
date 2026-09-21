import { NavLink, Outlet } from "react-router-dom";
import { useStrategyConfig } from "@/hooks/useStrategyConfig";
import { useEffect, useState } from "react";
import { marketProvider } from "@/providers/HttpWsProvider";
import { AutoPaperToggle } from "@/components/layout/AutoPaperToggle";

const links = [
  { to: "/", label: "行情", end: true },
  { to: "/strategy", label: "策略" },
  { to: "/trade", label: "交易" },
  { to: "/backtest", label: "回测" },
  { to: "/alerts", label: "告警" },
  { to: "/settings", label: "设置" },
];

export function AppShell() {
  const { tradingState, autoPaperOrders, setAutoPaperOrders } = useStrategyConfig();
  const [provider, setProvider] = useState("…");
  const [liveOff, setLiveOff] = useState(true);

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
    <div className="app-shell">
      <header className="topbar">
        <div className="brand">
          <span className="logo">AUU</span>
          <span className="sub">Pump.fun · 纸面曲线</span>
        </div>
        <nav>
          {links.map((l) => (
            <NavLink key={l.to} to={l.to} end={l.end} className={({ isActive }) => (isActive ? "active" : "")}>
              {l.label}
            </NavLink>
          ))}
        </nav>
        <div className="topbar-status">
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
            {liveOff ? "LIVE OFF · LIVE_DISABLED" : "LIVE CHECKLIST"}
          </div>
        </div>
      </header>
      <main className="main">
        <Outlet />
      </main>
    </div>
  );
}
