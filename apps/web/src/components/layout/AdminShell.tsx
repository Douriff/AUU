import { Link, NavLink, Outlet, useLocation, useNavigate } from "react-router-dom";
import { useEffect, useRef, useState } from "react";
import { AUTH_REQUIRED_EVENT, marketProvider } from "@/providers/HttpWsProvider";
import { Brand } from "@/components/ui/Brand";
import { LangSwitch } from "@/components/ui/LangSwitch";
import { useTranslation } from "react-i18next";
import { onExplicitLang, reconcileAccountLocale, setLang } from "@/i18n";

const AUTH_PATHS = new Set(["/login", "/register", "/forgot-password", "/status"]);

type Gate = "loading" | "anon" | "ok" | "forbidden";

const adminNav = [
  { to: "/admin", end: true, label: "hub" },
  { to: "/admin/users", label: "users" },
  { to: "/admin/ledger", label: "ledger" },
  { to: "/admin/topups", label: "topups" },
  { to: "/admin/trading", label: "trading" },
  { to: "/admin/withdrawals", label: "withdrawals" },
  { to: "/admin/flags", label: "flags" },
  { to: "/admin/exchange", label: "exchange" },
] as const;

/** Dedicated admin product surface — no market ticker, trade panel, or paper/points switch. */
export function AdminShell() {
  const [gate, setGate] = useState<Gate>("loading");
  const [who, setWho] = useState("");
  const navigate = useNavigate();
  const location = useLocation();
  const gateRef = useRef<Gate>("loading");
  gateRef.current = gate;
  const { t } = useTranslation();

  const refreshWho = () => {
    marketProvider
      .getMe()
      .then((me) => {
        if (me.user) {
          const { apply, push } = reconcileAccountLocale(me.user);
          if (apply) void setLang(apply, { explicit: true, silent: true, ts: Number(me.user.locale_ts) || Date.now() });
          else if (push) void marketProvider.setLocale(push).catch(() => undefined);
        }
        setWho(me.user?.display_name || me.user?.name || "");
        if (me.auth_enabled && !me.user) {
          setGate("anon");
          return;
        }
        const admin = !me.auth_enabled || Boolean(me.user?.is_admin);
        setGate(admin ? "ok" : "forbidden");
      })
      .catch(() => setGate((g) => (g === "loading" ? "ok" : g)));
  };

  useEffect(() => {
    refreshWho();
  }, [location.pathname]);

  useEffect(
    () =>
      onExplicitLang((lang) => {
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
      <div className="auth-screen is-card">
        <main className="auth-screen-body" />
      </div>
    );
  }

  if (gate === "forbidden") {
    return (
      <div className="admin-shell">
        <header className="admin-top">
          <Link to="/" className="topnav-brand" aria-label={t("nav.homeAria")}>
            <Brand />
            <span className="admin-badge">{t("admin.badge")}</span>
          </Link>
          <Link className="admin-back" to="/">
            {t("admin.backToBoard")}
          </Link>
        </header>
        <main className="admin-main">
          <div className="shell-page pro-page">
            <header className="pro-head">
              <h1>{t("admin.forbiddenTitle")}</h1>
              <p>{t("admin.forbiddenSub")}</p>
            </header>
            <Link className="td-link" to="/">
              {t("admin.backToBoard")} ›
            </Link>
          </div>
        </main>
      </div>
    );
  }

  return (
    <div className="admin-shell">
      <header className="admin-top">
        <Link to="/admin" className="topnav-brand" aria-label={t("admin.title")}>
          <Brand />
          <span className="admin-badge">{t("admin.badge")}</span>
        </Link>
        <nav className="admin-top-links" aria-label={t("admin.navAria")}>
          {adminNav.map((i) => (
            <NavLink key={i.to} to={i.to} end={"end" in i && i.end} className={({ isActive }) => (isActive ? "is-active" : "")}>
              {t(`admin.nav.${i.label}`)}
            </NavLink>
          ))}
        </nav>
        <div className="admin-top-right">
          <LangSwitch compact />
          {who ? <span className="admin-who muted">{who}</span> : null}
          <Link className="admin-back" to="/">
            {t("admin.backToBoard")}
          </Link>
        </div>
      </header>
      <div className="admin-body">
        <aside className="admin-side" aria-label={t("admin.navAria")}>
          <p className="admin-side-label">{t("admin.title")}</p>
          {adminNav.map((i) => (
            <NavLink key={i.to} to={i.to} end={"end" in i && i.end} className={({ isActive }) => (isActive ? "is-active" : "")}>
              {t(`admin.nav.${i.label}`)}
            </NavLink>
          ))}
          <div className="admin-side-foot">
            <Link to="/">{t("admin.backToBoard")}</Link>
          </div>
        </aside>
        <main className="admin-main">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
