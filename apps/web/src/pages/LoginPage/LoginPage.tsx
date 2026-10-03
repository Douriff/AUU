import { useEffect, useState, type FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";
import { marketProvider } from "@/providers/HttpWsProvider";
import type { AuthMe } from "@/types/contracts";
import { AuthHead } from "./AuthHead";

export function LoginPage() {
  const navigate = useNavigate();
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
      .catch((e) => setError(e instanceof Error ? e.message : "无法读取账户"));
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
      setError(e instanceof Error ? e.message : "登录失败");
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
      const msg = e instanceof Error ? e.message : "验证失败";
      setError(msg);
      if (msg.includes("过期")) setTicket("");
    } finally {
      setBusy(false);
    }
  }

  const authOn = Boolean(me?.auth_enabled);

  return (
    <div className="auth-page">
      <section className="auth-card">
        <AuthHead title="登录" sub="用邮箱或用户名登录。纸面交易：没有充值、没有提现、没有实盘下单。" />
        {me && !authOn && (
          <p className="td-note">
            当前是本地单用户模式，不需要登录。直接去 <Link to="/">行情</Link> 或 <Link to="/?symbol=BTC">交易</Link>。
          </p>
        )}
        {authOn && ticket && (
          <form onSubmit={(event) => void submitCode(event)}>
            <p className="td-note">此账户已开启两步验证。请输入验证器 App 里的 6 位数字，或一个恢复码（形如 abcde-fghjk）。</p>
            <label>
              两步验证码 / 恢复码
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
              {busy ? "验证中…" : "验证并登录"}
            </button>
            <button type="button" className="td-link" onClick={() => (setTicket(""), setError(""))}>
              返回重新输入密码
            </button>
          </form>
        )}
        {authOn && !ticket && (
          <form onSubmit={(event) => void submit(event)}>
            <label>
              邮箱或用户名
              <input value={name} onChange={(e) => setName(e.target.value)} autoComplete="username" autoCapitalize="none" spellCheck={false} required />
            </label>
            <label>
              密码
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
              {busy ? "提交中…" : "登录"}
            </button>
            {me?.signup_allowed && (
              <p className="td-note">
                还没有账户？<Link to="/register">去注册</Link>
              </p>
            )}
            {me?.email_verify && (
              <p className="td-note">
                忘记密码？<Link to="/forgot-password">用邮箱验证码重置</Link>
              </p>
            )}
            <p className="td-note">
              <Link to="/status">系统状态</Link>（无需登录）
            </p>
          </form>
        )}
        {error && !authOn && <p className="td-block">{error}</p>}
      </section>
    </div>
  );
}
