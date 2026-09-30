import { NavLink, Outlet, useLocation, useNavigate } from "react-router-dom";
import { useStrategyConfig } from "@/hooks/useStrategyConfig";
import { useEffect, useState, type ReactNode } from "react";
import { AUTH_REQUIRED_EVENT, marketProvider } from "@/providers/HttpWsProvider";
import { AutoPaperToggle } from "@/components/layout/AutoPaperToggle";

const primary = [
  { to: "/console", label: "控制台", icon: "term" },
  { to: "/", label: "总览", title: "盘面", end: true, icon: "grid" },
  { to: "/markets", label: "市场", icon: "bars" },
  { to: "/trade", label: "交易", icon: "swap" },
  { to: "/majors", label: "大盘", icon: "globe" },
  { to: "/positions", label: "持仓", icon: "bag" },
  { to: "/leaderboard", label: "排行榜", icon: "rank" },
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
    globe: (
      <>
        <circle cx="12" cy="12" r="8" />
        <path d="M4 12h16" />
        <path d="M12 4c2.4 2.6 2.4 13.4 0 16" />
        <path d="M12 4c-2.4 2.6-2.4 13.4 0 16" />
      </>
    ),
    swap: (
      <>
        <path d="M7 7h11" />
        <path d="M15 4l3 3-3 3" />
        <path d="M17 17H6" />
        <path d="M9 14l-3 3 3 3" />
      </>
    ),
    term: (
      <>
        <rect x="3" y="4" width="18" height="16" rx="2" />
        <path d="M7 9l3 3-3 3" />
        <path d="M12 15h5" />
      </>
    ),
    rank: (
      <>
        <path d="M7 20V10" />
        <path d="M12 20V4" />
        <path d="M17 20v-6" />
        <path d="M5 20h14" />
      </>
    ),
  };
  return <svg {...common}>{paths[name]}</svg>;
}

const AUTH_PATHS = new Set(["/login", "/register", "/forgot-password"]);

type Gate = "loading" | "anon" | "ok";

/** Login gate: with accounts on, anonymous visitors only see login/register/forgot-password. */
export function AppShell() {
  const [gate, setGate] = useState<Gate>("loading");
  const [who, setWho] = useState("");
  const [authOn, setAuthOn] = useState(false);
  const [isAdmin, setIsAdmin] = useState(true);
  const navigate = useNavigate();
  const location = useLocation();

  const refreshWho = () => {
    marketProvider
      .getMe()
      .then((me) => {
        setAuthOn(me.auth_enabled);
        setWho(me.user?.display_name || me.user?.name || "");
        setIsAdmin(!me.auth_enabled || Boolean(me.user?.is_admin));
        setGate(me.auth_enabled && !me.user ? "anon" : "ok");
      })
      .catch(() => setGate((g) => (g === "loading" ? "ok" : g)));
  };

  useEffect(() => {
    refreshWho();
  }, [location.pathname]);

  useEffect(() => {
    const onAuthRequired = () => refreshWho();
    window.addEventListener(AUTH_REQUIRED_EVENT, onAuthRequired);
    return () => window.removeEventListener(AUTH_REQUIRED_EVENT, onAuthRequired);
  }, []);

  useEffect(() => {
    if (gate === "anon" && !AUTH_PATHS.has(location.pathname)) {
      navigate("/login", { replace: true });
    }
  }, [gate, location.pathname, navigate]);

  if (gate === "loading") {
    return <div className="auth-screen" aria-busy="true" />;
  }
  if (gate === "anon") {
    return (
      <div className="auth-screen">
        <div className="auth-screen-brand">
          <span className="side-mark" aria-hidden="true">
            A
          </span>
          <span>AUU · Pump.fun 纸面终端</span>
        </div>
        <main className="auth-screen-body">{AUTH_PATHS.has(location.pathname) ? <Outlet /> : null}</main>
      </div>
    );
  }
  return (
    <ShellFrame
      who={who}
      authOn={authOn}
      isAdmin={isAdmin}
      onLogout={() => {
        void marketProvider.logout().finally(() => {
          setWho("");
          setGate("anon");
          navigate("/login", { replace: true });
        });
      }}
    />
  );
}

function ShellFrame({
  who,
  authOn,
  isAdmin,
  onLogout,
}: {
  who: string;
  authOn: boolean;
  isAdmin: boolean;
  onLogout: () => void;
}) {
  const { tradingState, autoPaperOrders, setAutoPaperOrders } = useStrategyConfig();
  const [provider, setProvider] = useState("…");
  const [marketData, setMarketData] = useState("");
  const [liveOff, setLiveOff] = useState(true);
  const [collapsed, setCollapsed] = useState(false);

  useEffect(() => {
    marketProvider
      .getHealth()
      .then((h) => {
        setProvider(h.provider);
        setMarketData(h.marketData || "");
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
            {marketData ? ` · ${marketData}` : ""}
          </span>
          <AutoPaperToggle
            compact
            label="自动纸面"
            checked={autoPaperOrders}
            disabled={!isAdmin}
            title={isAdmin ? undefined : "只有管理员可以切换系统自动纸面"}
            onChange={(v) => void setAutoPaperOrders(v).catch(() => undefined)}
          />
          <div className="mode-badge">PAPER · PUMP.FUN</div>
          <div className="mode-badge live-off" title="liveEnabled=false · LIVE_DISABLED">
            {liveOff ? "LIVE OFF" : "LIVE CHECKLIST"}
          </div>
          {authOn && who && (
            <button
              type="button"
              className="mode-badge auth-chip"
              onClick={onLogout}
            >
              {who} · 退出
            </button>
          )}
          {authOn && !who && (
            <NavLink to="/login" className="mode-badge auth-chip">
              登录
            </NavLink>
          )}
        </header>
        <main className="main">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
