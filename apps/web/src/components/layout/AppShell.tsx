import { NavLink, Outlet } from "react-router-dom";
import { useStrategyConfig } from "@/hooks/useStrategyConfig";
import { useEffect, useState } from "react";
import { marketProvider } from "@/providers/HttpWsProvider";

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

  useEffect(() => {
    marketProvider
      .getHealth()
      .then((h) => setProvider(h.provider))
      .catch(() => undefined);
  }, []);

  return (
    <div className="app-shell">
      <header className="topbar">
        <div className="brand">
          <span className="logo">AUU</span>
          <span className="sub">模因币量化 · 纸面终端</span>
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
          <label className="auto-paper-toggle compact">
            <input
              type="checkbox"
              checked={autoPaperOrders}
              onChange={(e) => void setAutoPaperOrders(e.target.checked)}
            />
            auto_paper
          </label>
          <div className="mode-badge">PAPER</div>
        </div>
      </header>
      <main className="main">
        <Outlet />
      </main>
    </div>
  );
}
