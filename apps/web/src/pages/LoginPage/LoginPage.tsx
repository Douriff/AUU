import { useEffect, useState, type FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";
import { marketProvider } from "@/providers/HttpWsProvider";
import type { AuthMe } from "@/types/contracts";

export function LoginPage() {
  const navigate = useNavigate();
  const [me, setMe] = useState<AuthMe | null>(null);
  const [name, setName] = useState("");
  const [password, setPassword] = useState("");
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
    setBusy(true);
    setError("");
    try {
      await marketProvider.login({ name: name.trim(), password });
      navigate("/trade");
    } catch (e) {
      setError(e instanceof Error ? e.message : "登录失败");
    } finally {
      setBusy(false);
    }
  }

  const authOn = Boolean(me?.auth_enabled);

  return (
    <div className="auth-page">
      <section className="auth-card">
        <header>
          <h1>登录</h1>
          <span className="td-badge paper">PAPER</span>
          <span className="td-badge live">LIVE OFF</span>
        </header>
        <p className="td-note">使用你自己的用户名和密码。没有充值、没有提现、没有实盘下单。</p>
        {me && !authOn && (
          <p className="td-note">
            当前是本地单用户模式，不需要登录。直接去 <Link to="/trade">交易</Link> 或 <Link to="/markets">市场</Link>。
          </p>
        )}
        {authOn && (
          <form onSubmit={(event) => void submit(event)}>
            <label>
              用户名
              <input value={name} onChange={(e) => setName(e.target.value)} autoComplete="username" required />
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
          </form>
        )}
        {error && !authOn && <p className="td-block">{error}</p>}
      </section>
    </div>
  );
}
