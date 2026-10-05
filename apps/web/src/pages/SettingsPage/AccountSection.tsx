import { useEffect, useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { marketProvider } from "@/providers/HttpWsProvider";
import type { AuthMe } from "@/types/contracts";
import { passwordRule, passwordProblem } from "@/lib/authRules";
import { Trans, useTranslation } from "react-i18next";
import { errText } from "@/i18n/errors";
import { SecuritySection } from "@/pages/SettingsPage/SecuritySection";

export function AccountSection() {
  const { t } = useTranslation();
  const [me, setMe] = useState<AuthMe | null>(null);
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [totp, setTotp] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [note, setNote] = useState("");

  useEffect(() => {
    marketProvider
      .getMe()
      .then(setMe)
      .catch(() => setMe(null));
  }, []);

  async function submit(event: FormEvent) {
    event.preventDefault();
    const problem = passwordProblem(next) ?? (next !== confirm ? t("auth.rule.mismatch") : null);
    if (problem) {
      setError(problem);
      setNote("");
      return;
    }
    setBusy(true);
    setError("");
    setNote("");
    try {
      await marketProvider.changePassword({
        current_password: current,
        new_password: next,
        new_password_confirm: confirm,
        ...(me?.user?.totp_enabled ? { totp_code: totp.trim() } : {}),
      });
      setTotp("");
      setCurrent("");
      setNext("");
      setConfirm("");
      setNote(t("account.updated"));
    } catch (e) {
      setError(errText(e, "account.failed"));
    } finally {
      setBusy(false);
    }
  }

  if (!me) return null;
  if (!me.auth_enabled) {
    return (
      <section className="settings-section">
        <h2>{t("account.title")}</h2>
        <p className="muted">{t("account.local")}</p>
      </section>
    );
  }
  if (!me.user) {
    return (
      <section className="settings-section">
        <h2>{t("account.title")}</h2>
        <p className="muted">
          <Trans i18nKey="account.anon" components={{ login: <Link to="/login" />, reg: <Link to="/register" /> }} />
        </p>
      </section>
    );
  }

  const label = me.user.display_name && me.user.display_name !== me.user.name ? `${me.user.display_name} (${me.user.name})` : me.user.name;

  return (
    <section className="settings-section">
      <h2>{t("account.change")}</h2>
      <p className="muted">{t("account.current", { label })}</p>
      <form className="auth-card settings-password" onSubmit={(event) => void submit(event)}>
        <label>
          {t("account.currentPw")}
          <input value={current} onChange={(e) => setCurrent(e.target.value)} type="password" autoComplete="current-password" required />
        </label>
        <label>
          {t("auth.newPassword")}
          <input value={next} onChange={(e) => setNext(e.target.value)} type="password" autoComplete="new-password" minLength={8} required />
          <small className="muted">{passwordRule()}</small>
        </label>
        <label>
          {t("auth.confirmNewPassword")}
          <input value={confirm} onChange={(e) => setConfirm(e.target.value)} type="password" autoComplete="new-password" minLength={8} required />
        </label>
        {me.user.totp_enabled && (
          <label>
            {t("auth.login.totpLabel")}
            <input value={totp} onChange={(e) => setTotp(e.target.value)} autoComplete="one-time-code" maxLength={16} required />
          </label>
        )}
        {error && <p className="td-block">{error}</p>}
        {note && <p className="td-note">{note}</p>}
        <button className="td-submit buy" type="submit" disabled={busy}>
          {busy ? t("common.submitting") : t("account.change")}
        </button>
      </form>
      <SecuritySection onChange={() => void marketProvider.getMe().then(setMe)} />
    </section>
  );
}
