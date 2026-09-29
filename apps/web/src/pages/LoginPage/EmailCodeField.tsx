import { useEffect, useState } from "react";
import { marketProvider } from "@/providers/HttpWsProvider";
import { emailProblem } from "@/lib/authRules";

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
      const res = await marketProvider.sendEmailCode({ email: email.trim(), purpose });
      setNote(`${res.message}（${res.email}）`);
      setCooldown(res.resend_after_sec || 60);
    } catch (e) {
      const message = e instanceof Error ? e.message : "验证码发送失败";
      onError(message);
      const wait = /请 (\d+) 秒后再试/.exec(message);
      if (wait) setCooldown(Number(wait[1]));
    } finally {
      setSending(false);
    }
  }

  return (
    <>
      <label>
        邮箱
        <input value={email} onChange={(e) => onEmail(e.target.value)} type="email" autoComplete="email" required />
        <small className="muted">支持 QQ 邮箱、163 邮箱等，验证码 10 分钟内有效</small>
      </label>
      <label>
        邮箱验证码
        <span className="email-code-row" style={{ display: "flex", gap: 8 }}>
          <input
            value={code}
            onChange={(e) => onCode(e.target.value.replace(/\D/g, "").slice(0, 6))}
            inputMode="numeric"
            autoComplete="one-time-code"
            placeholder="6 位数字"
            required
            style={{ flex: 1 }}
          />
          <button type="button" className="td-submit" onClick={() => void send()} disabled={sending || cooldown > 0}>
            {sending ? "发送中…" : cooldown > 0 ? `${cooldown} 秒后可重发` : "发送验证码"}
          </button>
        </span>
        {note && <small className="muted">{note}</small>}
      </label>
    </>
  );
}
