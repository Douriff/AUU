import { useEffect, useState, type FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";
import { marketProvider } from "@/providers/HttpWsProvider";
import type { AuthMe } from "@/types/contracts";
import {
  nameRule,
  passwordRule,
  codeProblem,
  emailProblem,
  nameProblem,
  passwordProblem,
} from "@/lib/authRules";
import { EmailCodeField } from "./EmailCodeField";
import { AuthHead } from "./AuthHead";
import { Trans, useTranslation } from "react-i18next";
import { errText } from "@/i18n/errors";

export function RegisterPage() {
  const navigate = useNavigate();
  const { t } = useTranslation();
  const [me, setMe] = useState<AuthMe | null>(null);
  const [name, setName] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [invite, setInvite] = useState("");
  const [email, setEmail] = useState("");
  const [emailCode, setEmailCode] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

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
    const verify = Boolean(me?.email_verify);
    // order of the form: email -> code -> username -> password
    const problem =
      (verify ? emailProblem(email) ?? codeProblem(emailCode) : null) ??
      nameProblem(name) ??
      passwordProblem(password) ??
      (password !== confirm ? t("auth.rule.mismatch") : null);
    if (problem) {
      setError(problem);
      return;
    }
    setBusy(true);
    setError("");
    try {
      await marketProvider.registerAccount({
        name: name.trim(),
        password,
        password_confirm: confirm,
        display_name: "",
        invite: invite.trim(),
        ...(verify ? { email: email.trim(), email_code: emailCode.trim() } : {}),
      });
      navigate("/");
    } catch (e) {
      setError(errText(e, "auth.register.failed"));
    } finally {
      setBusy(false);
    }
  }

  const authOn = Boolean(me?.auth_enabled);
  const open = Boolean(me?.signup_allowed);

  return (
    <div className="auth-page">
      <section className="auth-card">
        <AuthHead title={t("auth.register.title")} sub={t("auth.register.sub")} />
        {me && !authOn && (
          <p className="td-note">
            <Trans i18nKey="auth.register.localMode" components={{ market: <Link to="/" /> }} />
          </p>
        )}
        {authOn && !open && <p className="td-note">{t("auth.register.closed")}</p>}
        {authOn && open && (
          <form onSubmit={(event) => void submit(event)}>
            {me?.email_verify ? (
              <EmailCodeField purpose="signup" email={email} code={emailCode} onEmail={setEmail} onCode={setEmailCode} onError={setError} />
            ) : (
              <p className="td-note">{t("auth.register.noEmailVerify")}</p>
            )}
            <label>
              {t("auth.username")}
              <input value={name} onChange={(e) => setName(e.target.value)} autoComplete="username" autoCapitalize="none" spellCheck={false} minLength={2} maxLength={32} required />
              <small className="muted">{nameRule()}</small>
            </label>
            <label>
              {t("auth.password")}
              <input value={password} onChange={(e) => setPassword(e.target.value)} type="password" autoComplete="new-password" minLength={8} required />
              <small className="muted">{passwordRule()}</small>
            </label>
            <label>
              {t("auth.confirmPassword")}
              <input value={confirm} onChange={(e) => setConfirm(e.target.value)} type="password" autoComplete="new-password" minLength={8} required />
            </label>
            {me?.invite_required && (
              <label>
                {t("auth.register.invite")}
                <input value={invite} onChange={(e) => setInvite(e.target.value)} autoCapitalize="none" required />
              </label>
            )}
            {error && <p className="td-block">{error}</p>}
            <button className="td-submit buy" type="submit" disabled={busy}>
              {busy ? t("common.submitting") : t("auth.register.submit")}
            </button>
            <p className="td-note">
              {t("auth.register.haveAccount")}<Link to="/login">{t("auth.toLogin")}</Link>
            </p>
          </form>
        )}
        {error && !authOn && <p className="td-block">{error}</p>}
      </section>
    </div>
  );
}
