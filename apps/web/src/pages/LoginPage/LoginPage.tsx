import { useEffect, useState, type FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";
import { marketProvider } from "@/providers/HttpWsProvider";
import type { AuthMe } from "@/types/contracts";

export function LoginPage() {
  const navigate = useNavigate();
  const [me, setMe] = useState<AuthMe | null>(null);
  const [mode, setMode] = useState<"login" | "register">("login");
  const [name, setName] = useState("");
  const [password, setPassword] = useState("");
  const [invite, setInvite] = useState("");
  const [startSol, setStartSol] = useState("100");
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
      if (mode === "register") {
        const start = Number(startSol);
        await marketProvider.registerAccount({
          name: name.trim(),
          password,
          invite: invite.trim(),
          start_sol: Number.isFinite(start) ? start : undefined,
        });
      } else {
        await marketProvider.login({ name: name.trim(), password });
      }
      navigate("/trade");
    } catch (e) {
      setError(e instanceof Error ? e.message : "提交失败");
    } finally {
      setBusy(false);
    }
  }

  const authOn = Boolean(me?.auth_enabled);
  const canRegister = Boolean(me?.signup_allowed);

  return (
    <div className="auth-page">
      <section className="auth-card">
        <header>
          <h1>{mode === "register" ? "注册纸面账户" : "登录"}</h1>
          <span className="td-badge paper">PAPER</span>
          <span className="td-badge live">LIVE OFF</span>
        </header>
        <p className="td-note">每人一个独立的纸面账户。没有充值、没有提现、没有实盘下单。</p>
        {me && !authOn && (
          <p className="td-note">
            当前是本地单用户模式，不需要登录。直接去 <Link to="/trade">交易</Link> 或 <Link to="/markets">市场</Link>。
          </p>
        )}
        {authOn && (
          <>
            <div className="auth-tabs">
              <button type="button" className={mode === "login" ? "is-on" : ""} onClick={() => setMode("login")}>
                登录
              </button>
              {canRegister && (
                <button type="button" className={mode === "register" ? "is-on" : ""} onClick={() => setMode("register")}>
                  注册
                </button>
              )}
            </div>
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
                  autoComplete={mode === "register" ? "new-password" : "current-password"}
                  minLength={8}
                  required
                />
              </label>
              {mode === "register" && me?.invite_required && (
                <label>
                  邀请码
                  <input value={invite} onChange={(e) => setInvite(e.target.value)} required />
                </label>
              )}
              {mode === "register" && (
                <label>
                  起始 SOL
                  <input value={startSol} onChange={(e) => setStartSol(e.target.value)} inputMode="decimal" />
                </label>
              )}
              {error && <p className="td-block">{error}</p>}
              <button className="td-submit buy" type="submit" disabled={busy}>
                {busy ? "提交中…" : mode === "register" ? "创建纸面账户" : "登录"}
              </button>
            </form>
          </>
        )}
        {error && !authOn && <p className="td-block">{error}</p>}
      </section>
    </div>
  );
}
