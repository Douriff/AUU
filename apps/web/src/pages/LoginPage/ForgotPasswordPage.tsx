import { useEffect, useState, type FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";
import { marketProvider } from "@/providers/HttpWsProvider";
import type { AuthMe } from "@/types/contracts";
import { passwordRule, codeProblem, emailProblem, passwordProblem } from "@/lib/authRules";
import { EmailCodeField } from "./EmailCodeField";
import { AuthHead } from "./AuthHead";
import { useTranslation } from "react-i18next";
import { errText } from "@/i18n/errors";

export function ForgotPasswordPage() {
  const navigate = useNavigate();
  const { t } = useTranslation();
  const [me, setMe] = useState<AuthMe | null>(null);
  const [email, setEmail] = useState("");
  const [code, setCode] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [done, setDone] = useState("");

  useEffect(() => {
    marketProvider
      .getMe()
      .then(setMe)
      .catch((e) => setError(errText(e, "auth.cantReadAccount")));
  }, []);

  async function submit(event: FormEvent) {
    event.preventDefault();
    const problem =
      emailProblem(email) ?? codeProblem(code) ?? passwordProblem(password) ?? (password !== confirm ? t("auth.rule.mismatch") : null);
    if (problem) {
      setError(problem);
      return;
    }
    setBusy(true);
    setError("");
    try {
      await marketProvider.resetPassword({
        email: email.trim(),
        code: code.trim(),
        new_password: password,
        new_password_confirm: confirm,
      });
      setDone(t("auth.reset.done"));
      setPassword("");
      setConfirm("");
      window.setTimeout(() => navigate("/login"), 1500);
    } catch (e) {
      setError(errText(e, "auth.reset.failed"));
    } finally {
      setBusy(false);
    }
  }

  const enabled = Boolean(me?.auth_enabled && me?.email_verify);

  return (
    <div className="auth-page">
      <section className="auth-card">
        <AuthHead title={t("auth.reset.title")} sub={t("auth.reset.sub")} />
        {me && !enabled && <p className="td-note">{t("auth.reset.disabled")}</p>}
        {enabled && (
          <form onSubmit={(event) => void submit(event)}>
            <EmailCodeField purpose="reset" email={email} code={code} onEmail={setEmail} onCode={setCode} onError={setError} />
            <label>
              {t("auth.newPassword")}
              <input
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                type="password"
                autoComplete="new-password"
                minLength={8}
                required
              />
              <small className="muted">{passwordRule()}</small>
            </label>
            <label>
              {t("auth.confirmNewPassword")}
              <input
                value={confirm}
                onChange={(e) => setConfirm(e.target.value)}
                type="password"
                autoComplete="new-password"
                minLength={8}
                required
              />
            </label>
            {error && <p className="td-block">{error}</p>}
            {done && <p className="td-note">{done}</p>}
            <button className="td-submit buy" type="submit" disabled={busy || Boolean(done)}>
              {busy ? t("common.submitting") : t("auth.reset.submit")}
            </button>
          </form>
        )}
        {error && !enabled && <p className="td-block">{error}</p>}
        <p className="td-note">
          {t("auth.reset.remembered")}<Link to="/login">{t("auth.toLogin")}</Link>
        </p>
      </section>
    </div>
  );
}
