import { Link, NavLink, Outlet, useLocation, useNavigate } from "react-router-dom";
import { useStrategyConfig } from "@/hooks/useStrategyConfig";
import { useEffect, useRef, useState, type ReactNode } from "react";
import { AUTH_REQUIRED_EVENT, marketProvider } from "@/providers/HttpWsProvider";
import { AutoPaperToggle } from "@/components/layout/AutoPaperToggle";
import { useLegacyMode } from "@/hooks/useLegacyMode";
import { Brand, ModeBadge } from "@/components/ui/Brand";
import { ColorToggle } from "@/components/ui/ColorToggle";
import type { MainstreamFreshness } from "@/types/mainstream";
import { useTranslation } from "react-i18next";
import i18n from "i18next";
import { LangSwitch } from "@/components/ui/LangSwitch";
import { onExplicitLang, reconcileAccountLocale, setLang } from "@/i18n";
import { fmtDate } from "@/i18n/format";

// Legacy pump nav (AUU_LEGACY_PUMP=on only). Labels are i18n keys under nav.legacy.*
const primary = [
  { to: "/console", label: "console", icon: "term" },
  { to: "/", label: "overview", end: true, icon: "grid" },
  { to: "/markets", label: "markets", icon: "bars" },
  { to: "/trade", label: "trade", icon: "swap" },
  { to: "/majors", label: "majors", icon: "globe" },
  { to: "/positions", label: "positions", icon: "bag" },
  { to: "/leaderboard", label: "leaderboard", icon: "rank" },
  { to: "/strategy", label: "strategy", icon: "sliders" },
  { to: "/review", label: "review", icon: "loop" },
  { to: "/settings", label: "settings", icon: "gear" },
] as const;

const more = [
  { to: "/market", label: "market" },
  { to: "/watch", label: "watch" },
  { to: "/backtest", label: "backtest" },
  { to: "/alerts", label: "alerts" },
];

/** Default (AUU_LEGACY_PUMP off): mainstream nav only; pump pages are archived. */
/** label = i18n key under nav.* */
type NavItem = { to: string; label: string; icon: string; match: (path: string, search: string) => boolean };
const LAST_SYMBOL_KEY = "auu.ms.last";
const isTrade = (p: string, q: string) => p === "/" && /(?:^|[?&])symbol=/.test(q);
const mainstreamNav: NavItem[] = [
  { to: "/", label: "market", icon: "bars", match: (p, q) => p === "/" && !isTrade(p, q) },
  { to: "/?symbol=BTC", label: "trade", icon: "swap", match: isTrade },
  { to: "/console", label: "strategy", icon: "term", match: (p) => p.startsWith("/console") },
  { to: "/performance", label: "performance", icon: "rank", match: (p) => p.startsWith("/performance") },
  { to: "/majors", label: "majors", icon: "globe", match: (p) => p.startsWith("/majors") },
  { to: "/news", label: "news", icon: "news", match: (p) => p.startsWith("/news") },
];
const mobileTabs: NavItem[] = [
  mainstreamNav[0],
  mainstreamNav[1],
  mainstreamNav[2],
  mainstreamNav[3],
  mainstreamNav[5],
  { to: "/settings", label: "me", icon: "user", match: (p) => p.startsWith("/settings") || p.startsWith("/leaderboard") || p.startsWith("/majors") },
];

function tradeHref(): string {
  try {
    const s = window.localStorage.getItem(LAST_SYMBOL_KEY);
    return s && /^[A-Z0-9]{1,20}$/.test(s) ? `/?symbol=${s}` : "/?symbol=BTC";
  } catch {
    return "/?symbol=BTC";
  }
}

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
        <circle cx="8" cy="7" r="2" fill="#0b0d10" />
        <circle cx="15" cy="12" r="2" fill="#0b0d10" />
        <circle cx="11" cy="17" r="2" fill="#0b0d10" />
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
    user: (
      <>
        <circle cx="12" cy="8.5" r="3.5" />
        <path d="M5 20c1.2-3.6 4-5.2 7-5.2s5.8 1.6 7 5.2" />
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
    news: (
      <>
        <path d="M5 4.5h11a1.5 1.5 0 0 1 1.5 1.5v12.5a1.5 1.5 0 0 0 3 0V9h-3" />
        <path d="M5 4.5v14A1.5 1.5 0 0 0 6.5 20h14" />
        <path d="M8 8.5h6M8 12h6M8 15.5h4" />
      </>
    ),
  };
  return <svg {...common}>{paths[name]}</svg>;
}

// pages an anonymous visitor may open (/status is the login-free status page)
const AUTH_PATHS = new Set(["/login", "/register", "/forgot-password", "/status"]);

type Gate = "loading" | "anon" | "ok";

/** Login gate: with accounts on, anonymous visitors only see login/register/forgot-password. */
export function AppShell() {
  const [gate, setGate] = useState<Gate>("loading");
  const [who, setWho] = useState("");
  const [authOn, setAuthOn] = useState(false);
  const [isAdmin, setIsAdmin] = useState(true);
  const navigate = useNavigate();
  const location = useLocation();
  const gateRef = useRef<Gate>("loading");
  gateRef.current = gate;

  const refreshWho = () => {
    marketProvider
      .getMe()
      .then((me) => {
        if (me.user) {
          // newest of (this device's explicit choice, the account's saved language) wins
          const { apply, push } = reconcileAccountLocale(me.user);
          if (apply) void setLang(apply, { explicit: true, silent: true, ts: Number(me.user.locale_ts) || Date.now() });
          else if (push) void marketProvider.setLocale(push).catch(() => undefined);
        }
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

  useEffect(
    () =>
      onExplicitLang((lang) => {
        // logged in: save on the account (silently ignored when anonymous / auth off)
        if (gateRef.current === "ok") void marketProvider.setLocale(lang).catch(() => undefined);
      }),
    [],
  );

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
    return (
      <div className="auth-screen" aria-busy="true">
        <div className="auth-screen-brand">
          <Brand />
        </div>
      </div>
    );
  }
  if (gate === "anon") {
    return (
      <div className={`auth-screen${location.pathname === "/status" ? " is-status" : " is-card"}`}>
        {location.pathname === "/status" ? (
          <div className="auth-screen-brand">
            <Brand />
            <ModeBadge />
            <LangSwitch compact />
          </div>
        ) : null}
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

function fmtClock(ms: number | null | undefined): string {
  if (!ms) return "—";
  return fmtDate(ms, { hour12: false, hour: "2-digit", minute: "2-digit" });
}

/** Data-source dot: green = fresh, amber = stale/partially blocked. Details on hover / tap. */
function DataDot({ f, provider }: { f: MainstreamFreshness | null; provider: string }) {
  const { t } = useTranslation();
  if (!f) return <span className="data-dot is-idle" title={provider ? t("shell.source", { src: provider }) : t("shell.sourceLoading")} />;
  const blocked = Object.keys(f.blocked || {});
  const warn = f.stale;
  const title = [
    t("shell.source", { src: f.exchange ? f.exchange.toUpperCase() : "—" }),
    t("shell.lastRefresh", { time: fmtClock(f.lastRefreshMs) }),
    f.stale ? (f.staleSeries.length ? t("shell.staleList", { list: f.staleSeries.join(", ") }) : t("shell.stale")) : t("shell.fresh"),
    blocked.length ? t("shell.blocked", { list: blocked.join(", ").toUpperCase() }) : "",
  ]
    .filter(Boolean)
    .join("\n");
  return (
    <span className={`data-dot${warn ? " is-warn" : ""}`} title={title}>
      <i aria-hidden="true" />
      <span className="data-dot-text">
        {f.exchange ? f.exchange.toUpperCase() : "—"} {f.stale ? t("shell.stale") : fmtClock(f.lastRefreshMs)}
      </span>
    </span>
  );
}


function UserMenu({ who, authOn, onLogout }: { who: string; authOn: boolean; onLogout: () => void }) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const box = useRef<HTMLDivElement>(null);
  const location = useLocation();
  useEffect(() => setOpen(false), [location.pathname, location.search]);
  useEffect(() => {
    if (!open) return;
    const off = (e: MouseEvent) => {
      if (box.current && !box.current.contains(e.target as Node)) setOpen(false);
    };
    const esc = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    window.addEventListener("mousedown", off);
    window.addEventListener("keydown", esc);
    return () => {
      window.removeEventListener("mousedown", off);
      window.removeEventListener("keydown", esc);
    };
  }, [open]);
  const initial = (who || t("shell.meInitial")).slice(0, 1).toUpperCase();
  return (
    <div className="um" ref={box}>
      <button type="button" className="um-btn" aria-haspopup="menu" aria-expanded={open} onClick={() => setOpen((v) => !v)} title={who || t("shell.accountTitle")}>
        <span className="um-av">{initial}</span>
        <span className="um-name">{who || t("shell.local")}</span>
        <span aria-hidden="true">▾</span>
      </button>
      {open ? (
        <div className="um-pop" role="menu">
          <Link role="menuitem" to="/settings">{t("shell.settings")}</Link>
          <Link role="menuitem" to="/leaderboard">{t("nav.leaderboard")}</Link>
          <div className="um-sep" />
          <div className="um-label">{t("color.label")}</div>
          <ColorToggle />
          {authOn && who ? (
            <>
              <div className="um-sep" />
              <button type="button" role="menuitem" className="um-out" onClick={onLogout}>
                {t("shell.logout")}
              </button>
            </>
          ) : null}
          {authOn && !who ? (
            <Link role="menuitem" to="/login">
              {t("auth.login.submit")}
            </Link>
          ) : null}
        </div>
      ) : null}
    </div>
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
  const legacy = useLegacyMode();
  const location = useLocation();
  const { t } = useTranslation();
  const [healthState, setHealthState] = useState("");
  const [provider, setProvider] = useState("");
  const [marketData, setMarketData] = useState("");
  const [liveOff, setLiveOff] = useState(true);
  const [fresh, setFresh] = useState<MainstreamFreshness | null>(null);

  useEffect(() => {
    let alive = true;
    const load = () =>
      marketProvider
        .getHealth()
        .then((h) => {
          if (!alive) return;
          setProvider(h.provider);
          setHealthState(h.trading_state || "");
          setMarketData(h.marketData || "");
          setLiveOff(h.liveDisabled !== false || h.liveEnabled === false);
          const m = (h as { mainstream?: MainstreamFreshness }).mainstream;
          if (m && typeof m === "object") setFresh(m);
        })
        .catch(() => undefined);
    void load();
    const t = window.setInterval(() => !document.hidden && void load(), 60_000);
    return () => {
      alive = false;
      window.clearInterval(t);
    };
  }, []);

  // Remember the last opened coin so the 交易 tab returns to it.
  const sym = new URLSearchParams(location.search).get("symbol");
  useEffect(() => {
    if (sym && /^[A-Za-z0-9]{1,20}$/.test(sym)) window.localStorage.setItem(LAST_SYMBOL_KEY, sym.toUpperCase());
  }, [sym]);

  const halted = healthState && !["ACTIVE", "active"].includes(healthState);
  const nav = legacy
    ? primary.map((i) => ({ to: i.to, label: `legacy.${i.label}`, icon: i.icon, match: (p: string) => ("end" in i && i.end ? p === i.to : p.startsWith(i.to)) }))
    : mainstreamNav;
  const href = (i: NavItem) => (i.label === "trade" && !legacy ? tradeHref() : i.to);

  return (
    <div className={`pro-shell${legacy ? " is-legacy" : ""}`}>
      <header className="topnav">
        <Link to="/" className="topnav-brand" aria-label={t("nav.homeAria")}>
          <Brand />
        </Link>
        <nav className="topnav-links" aria-label={t("nav.mainAria")}>
          {nav.map((i) => (
            <Link key={i.to + i.label} to={href(i)} className={i.match(location.pathname, location.search) ? "is-active" : ""} aria-current={i.match(location.pathname, location.search) ? "page" : undefined}>
              {t(`nav.${i.label}`)}
            </Link>
          ))}
          {legacy
            ? more.map((i) => (
                <NavLink key={i.to} to={i.to} className={({ isActive }) => (isActive ? "is-active" : "")}>
                  {t(`nav.legacy.${i.label}`)}
                </NavLink>
              ))
            : null}
        </nav>
        <div className="topnav-right">
          {legacy ? <LegacyControls isAdmin={isAdmin} provider={provider} marketData={marketData} /> : null}
          {halted ? (
            <span className="risk-pill" title={t("shell.riskTitle")}>
              {t("shell.risk")} {healthState}
            </span>
          ) : null}
          <DataDot f={fresh} provider={provider} />
          <ModeBadge liveOff={liveOff} />
          <LangSwitch compact />
          <UserMenu who={who} authOn={authOn} onLogout={onLogout} />
        </div>
      </header>
      <main className="main pro-main">
        <Outlet />
      </main>
      {legacy ? null : (
        <nav className="tabbar" aria-label={t("nav.tabAria")}>
          {mobileTabs.map((i) => {
            const on = i.match(location.pathname, location.search);
            return (
              <Link key={i.label} to={href(i)} className={on ? "is-active" : ""} aria-current={on ? "page" : undefined}>
                <Icon name={i.icon} />
                <span>{t(`nav.${i.label}`)}</span>
              </Link>
            );
          })}
        </nav>
      )}
    </div>
  );
}

/** pump-paper-v1 status + autopaper toggle (legacy stack only; polls the strategy route). */
function LegacyControls({ isAdmin, provider, marketData }: { isAdmin: boolean; provider: string; marketData: string }) {
  const { tradingState, autoPaperOrders, setAutoPaperOrders } = useStrategyConfig();
  const t = i18n.t.bind(i18n);
  return (
    <>
      <span className="muted topbar-provider" title={`${t("shell.source", { src: provider })}${marketData ? ` · ${marketData}` : ""} · ${t("shell.risk")} ${tradingState}`}>
        {provider}
      </span>
      <AutoPaperToggle
        compact
        label={t("shell.autoPaper")}
        checked={autoPaperOrders}
        disabled={!isAdmin}
        title={isAdmin ? undefined : t("shell.autoPaperAdmin")}
        onChange={(v) => void setAutoPaperOrders(v).catch(() => undefined)}
      />
    </>
  );
}
