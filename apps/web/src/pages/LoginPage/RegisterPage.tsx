import { useEffect, useState, type FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";
import { marketProvider } from "@/providers/HttpWsProvider";
import type { AuthMe } from "@/types/contracts";
import {
  NAME_RULE,
  PASSWORD_RULE,
  codeProblem,
  emailProblem,
  nameProblem,
  passwordProblem,
} from "@/lib/authRules";
import { EmailCodeField } from "./EmailCodeField";
import { AuthHead } from "./AuthHead";

export function RegisterPage() {
  const navigate = useNavigate();
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
      .catch((e) => setError(e instanceof Error ? e.message : "无法读取账户"));
  }, [navigate]);

  async function submit(event: FormEvent) {
    event.preventDefault();
    const verify = Boolean(me?.email_verify);
    // order of the form: email -> code -> username -> password
    const problem =
      (verify ? emailProblem(email) ?? codeProblem(emailCode) : null) ??
      nameProblem(name) ??
      passwordProblem(password) ??
      (password !== confirm ? "两次密码不一致" : null);
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
      setError(e instanceof Error ? e.message : "注册失败");
    } finally {
      setBusy(false);
    }
  }

  const authOn = Boolean(me?.auth_enabled);
  const open = Boolean(me?.signup_allowed);

  return (
    <div className="auth-page">
      <section className="auth-card">
        <AuthHead title="注册" sub="用邮箱注册纸面交易账户：先收验证码，再设置用户名和密码。" />
        {me && !authOn && (
          <p className="td-note">
            当前是本地单用户模式，不需要注册。直接去 <Link to="/">行情</Link>。
          </p>
        )}
        {authOn && !open && <p className="td-note">注册已关闭。</p>}
        {authOn && open && (
          <form onSubmit={(event) => void submit(event)}>
            {me?.email_verify ? (
              <EmailCodeField purpose="signup" email={email} code={emailCode} onEmail={setEmail} onCode={setEmailCode} onError={setError} />
            ) : (
              <p className="td-note">当前未开启邮箱验证，直接设置用户名和密码。</p>
            )}
            <label>
              用户名
              <input value={name} onChange={(e) => setName(e.target.value)} autoComplete="username" autoCapitalize="none" spellCheck={false} minLength={2} maxLength={32} required />
              <small className="muted">{NAME_RULE}</small>
            </label>
            <label>
              密码
              <input value={password} onChange={(e) => setPassword(e.target.value)} type="password" autoComplete="new-password" minLength={8} required />
              <small className="muted">{PASSWORD_RULE}</small>
            </label>
            <label>
              确认密码
              <input value={confirm} onChange={(e) => setConfirm(e.target.value)} type="password" autoComplete="new-password" minLength={8} required />
            </label>
            {me?.invite_required && (
              <label>
                邀请码
                <input value={invite} onChange={(e) => setInvite(e.target.value)} autoCapitalize="none" required />
              </label>
            )}
            {error && <p className="td-block">{error}</p>}
            <button className="td-submit buy" type="submit" disabled={busy}>
              {busy ? "提交中…" : "注册"}
            </button>
            <p className="td-note">
              已有账户？<Link to="/login">去登录</Link>
            </p>
          </form>
        )}
        {error && !authOn && <p className="td-block">{error}</p>}
      </section>
    </div>
  );
}
