import { useEffect, useState } from "react";
import { marketProvider } from "@/providers/HttpWsProvider";
import { emailProblem } from "@/lib/authRules";
import { useTranslation } from "react-i18next";
import { ApiError } from "@/providers/HttpWsProvider";
import { errText } from "@/i18n/errors";

type Props = {
  purpose: "signup" | "reset";
  email: string;
  code: string;
  onEmail: (value: string) => void;
  onCode: (value: string) => void;
  onError: (message: string) => void;
};

/** Email input + 发送验证码 button with a 60 s cooldown, plus the 6-digit code input. */
export function EmailCodeField({ purpose, email, code, onEmail, onCode, onError }: Props) {
  const { t, i18n } = useTranslation();
  const [cooldown, setCooldown] = useState(0);
  const [sending, setSending] = useState(false);
  const [note, setNote] = useState("");

  useEffect(() => {
    if (cooldown <= 0) return;
    const timer = window.setTimeout(() => setCooldown((left) => left - 1), 1000);
    return () => window.clearTimeout(timer);
  }, [cooldown]);

  async function send() {
    const problem = emailProblem(email);
    if (problem) {
      onError(problem);
      return;
    }
    setSending(true);
    onError("");
    setNote("");
    try {
      const res = await marketProvider.sendEmailCode({ email: email.trim(), purpose, lang: i18n.language });
      setNote(t(purpose === "reset" ? "auth.code.sentReset" : "auth.code.sent", { email: res.email, min: Math.max(1, Math.round((res.ttl_sec || 600) / 60)) }));
      setCooldown(res.resend_after_sec || 60);
    } catch (e) {
      onError(errText(e, "auth.code.sendFailed"));
      const wait = e instanceof ApiError && e.code === "EMAIL_THROTTLE" ? e.retryAfter ?? 0 : 0;
      if (wait) setCooldown(wait);
    } finally {
      setSending(false);
    }
  }

  return (
    <>
      <label>
        {t("auth.email")}
        <input value={email} onChange={(e) => onEmail(e.target.value)} type="email" autoComplete="email" required />
        <small className="muted">{t("auth.code.hint")}</small>
      </label>
      <label>
        {t("auth.code.label")}
        <span className="email-code-row" style={{ display: "flex", gap: 8 }}>
          <input
            value={code}
            onChange={(e) => onCode(e.target.value.replace(/\D/g, "").slice(0, 6))}
            inputMode="numeric"
            autoComplete="one-time-code"
            placeholder={t("auth.code.placeholder")}
            required
            style={{ flex: 1 }}
          />
          <button type="button" className="td-submit" onClick={() => void send()} disabled={sending || cooldown > 0}>
            {sending ? t("auth.code.sending") : cooldown > 0 ? t("auth.code.resendIn", { n: cooldown }) : t("auth.code.send")}
          </button>
        </span>
        {note && <small className="muted">{note}</small>}
      </label>
    </>
  );
}
