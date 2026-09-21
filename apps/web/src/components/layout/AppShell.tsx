import { NavLink, Outlet } from "react-router-dom";

const links = [
  { to: "/", label: "行情", end: true },
  { to: "/strategy", label: "策略" },
  { to: "/trade", label: "交易" },
  { to: "/backtest", label: "回测" },
  { to: "/alerts", label: "告警" },
  { to: "/settings", label: "设置" },
];

export function AppShell() {
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
        <div className="mode-badge">PAPER · PUMP.FUN</div>
      </header>
      <main className="main">
        <Outlet />
      </main>
    </div>
  );
}
