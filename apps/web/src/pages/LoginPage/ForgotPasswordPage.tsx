import { useEffect, useState, type FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";
import { marketProvider } from "@/providers/HttpWsProvider";
import type { AuthMe } from "@/types/contracts";
import { PASSWORD_RULE, codeProblem, emailProblem, passwordProblem } from "@/lib/authRules";
import { EmailCodeField } from "./EmailCodeField";

export function ForgotPasswordPage() {
  const navigate = useNavigate();
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
      .catch((e) => setError(e instanceof Error ? e.message : "无法读取账户"));
  }, []);

  async function submit(event: FormEvent) {
    event.preventDefault();
    const problem =
      emailProblem(email) ?? codeProblem(code) ?? passwordProblem(password) ?? (password !== confirm ? "两次密码不一致" : null);
    if (problem) {
      setError(problem);
      return;
    }
    setBusy(true);
    setError("");
    try {
      const res = await marketProvider.resetPassword({
        email: email.trim(),
        code: code.trim(),
        new_password: password,
        new_password_confirm: confirm,
      });
      setDone(res.message || "密码已重置，请使用新密码登录");
      setPassword("");
      setConfirm("");
      window.setTimeout(() => navigate("/login"), 1500);
    } catch (e) {
      setError(e instanceof Error ? e.message : "重置失败");
    } finally {
      setBusy(false);
    }
  }

  const enabled = Boolean(me?.auth_enabled && me?.email_verify);

  return (
    <div className="auth-page">
      <section className="auth-card">
        <header>
          <h1>忘记密码</h1>
          <span className="td-badge paper">PAPER</span>
          <span className="td-badge live">LIVE OFF</span>
        </header>
        <p className="td-note">输入注册时使用的邮箱，获取验证码后设置新密码。重置后所有已登录的设备都需要重新登录。</p>
        {me && !enabled && <p className="td-note">当前未开启邮箱验证，无法通过邮箱重置密码。请联系管理员。</p>}
        {enabled && (
          <form onSubmit={(event) => void submit(event)}>
            <EmailCodeField purpose="reset" email={email} code={code} onEmail={setEmail} onCode={setCode} onError={setError} />
            <label>
              新密码
              <input
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                type="password"
                autoComplete="new-password"
                minLength={8}
                required
              />
              <small className="muted">{PASSWORD_RULE}</small>
            </label>
            <label>
              确认新密码
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
              {busy ? "提交中…" : "重置密码"}
            </button>
          </form>
        )}
        {error && !enabled && <p className="td-block">{error}</p>}
        <p className="td-note">
          想起来了？<Link to="/login">去登录</Link>
        </p>
      </section>
    </div>
  );
}
