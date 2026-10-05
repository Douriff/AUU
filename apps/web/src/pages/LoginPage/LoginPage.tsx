import { useEffect, useState, type FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";
import { marketProvider } from "@/providers/HttpWsProvider";
import type { AuthMe } from "@/types/contracts";
import { AuthHead } from "./AuthHead";
import { Trans, useTranslation } from "react-i18next";
import { ApiError } from "@/providers/HttpWsProvider";
import { errText } from "@/i18n/errors";

export function LoginPage() {
  const navigate = useNavigate();
  const { t } = useTranslation();
  const [me, setMe] = useState<AuthMe | null>(null);
  const [name, setName] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [ticket, setTicket] = useState("");
  const [code, setCode] = useState("");

  useEffect(() => {
    marketProvider
      .getMe()
      .then((next) => {
        setMe(next);
        if (next.user) navigate("/leaderboard", { replace: true });
      })
      .catch((e) => setError(errText(e, "auth.cantReadAccount")));
  }, [navigate]);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      const res = await marketProvider.login({ name: name.trim(), password });
      if ("totp_required" in res && res.totp_required) {
        setTicket(res.ticket);
        setCode("");
        return;
      }
      navigate("/");
    } catch (e) {
      setError(errText(e, "auth.loginFailed"));
    } finally {
      setBusy(false);
    }
  }

  async function submitCode(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      await marketProvider.loginTotp({ ticket, code: code.trim() });
      navigate("/");
    } catch (e) {
      setError(errText(e, "auth.verifyFailed"));
      if (e instanceof ApiError && e.code === "TOTP_TICKET") setTicket("");
    } finally {
      setBusy(false);
    }
  }

  const authOn = Boolean(me?.auth_enabled);

  return (
    <div className="auth-page">
      <section className="auth-card">
        <AuthHead title={t("auth.login.title")} sub={t("auth.login.sub")} />
        {me && !authOn && (
          <p className="td-note">
            <Trans i18nKey="auth.login.localMode" components={{ market: <Link to="/" />, trade: <Link to="/?symbol=BTC" /> }} />
          </p>
        )}
        {authOn && ticket && (
          <form onSubmit={(event) => void submitCode(event)}>
            <p className="td-note">{t("auth.login.totpNote")}</p>
            <label>
              {t("auth.login.totpLabel")}
              <input
                value={code}
                onChange={(e) => setCode(e.target.value)}
                autoComplete="one-time-code"
                inputMode="text"
                autoFocus
                maxLength={16}
                required
              />
            </label>
            {error && <p className="td-block">{error}</p>}
            <button className="td-submit buy" type="submit" disabled={busy}>
              {busy ? t("auth.login.verifying") : t("auth.login.verifySubmit")}
            </button>
            <button type="button" className="td-link" onClick={() => (setTicket(""), setError(""))}>
              {t("auth.login.backToPassword")}
            </button>
          </form>
        )}
        {authOn && !ticket && (
          <form onSubmit={(event) => void submit(event)}>
            <label>
              {t("auth.login.nameLabel")}
              <input value={name} onChange={(e) => setName(e.target.value)} autoComplete="username" autoCapitalize="none" spellCheck={false} required />
            </label>
            <label>
              {t("auth.password")}
              <input
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                type="password"
                autoComplete="current-password"
                minLength={8}
                required
              />
            </label>
            {error && <p className="td-block">{error}</p>}
            <button className="td-submit buy" type="submit" disabled={busy}>
              {busy ? t("common.submitting") : t("auth.login.submit")}
            </button>
            {me?.signup_allowed && (
              <p className="td-note">
                {t("auth.login.noAccount")}<Link to="/register">{t("auth.login.toRegister")}</Link>
              </p>
            )}
            {me?.email_verify && (
              <p className="td-note">
                {t("auth.login.forgot")}<Link to="/forgot-password">{t("auth.login.toReset")}</Link>
              </p>
            )}
            <p className="td-note">
              <Link to="/status">{t("auth.login.status")}</Link> {t("auth.login.noLoginNeeded")}
            </p>
          </form>
        )}
        {error && !authOn && <p className="td-block">{error}</p>}
      </section>
    </div>
  );
}
