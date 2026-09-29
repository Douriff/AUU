import { useEffect, useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { marketProvider } from "@/providers/HttpWsProvider";
import type { AuthMe } from "@/types/contracts";
import { PASSWORD_RULE, passwordProblem } from "@/lib/authRules";

export function AccountSection() {
  const [me, setMe] = useState<AuthMe | null>(null);
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
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
    const problem = passwordProblem(next) ?? (next !== confirm ? "两次密码不一致" : null);
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
      });
      setCurrent("");
      setNext("");
      setConfirm("");
      setNote("密码已更新");
    } catch (e) {
      setError(e instanceof Error ? e.message : "修改失败");
    } finally {
      setBusy(false);
    }
  }

  if (!me) return null;
  if (!me.auth_enabled) {
    return (
      <section className="settings-section">
        <h2>账户</h2>
        <p className="muted">本地单用户模式，不需要登录或修改密码。</p>
      </section>
    );
  }
  if (!me.user) {
    return (
      <section className="settings-section">
        <h2>账户</h2>
        <p className="muted">
          <Link to="/login">登录</Link> 后可以修改密码。还没有账户可以 <Link to="/register">注册</Link>。
        </p>
      </section>
    );
  }

  const label = me.user.display_name && me.user.display_name !== me.user.name ? `${me.user.display_name}（${me.user.name}）` : me.user.name;

  return (
    <section className="settings-section">
      <h2>修改密码</h2>
      <p className="muted">当前账户 {label}。密码只以哈希保存，管理员看不到。</p>
      <form className="auth-card settings-password" onSubmit={(event) => void submit(event)}>
        <label>
          当前密码
          <input value={current} onChange={(e) => setCurrent(e.target.value)} type="password" autoComplete="current-password" required />
        </label>
        <label>
          新密码
          <input value={next} onChange={(e) => setNext(e.target.value)} type="password" autoComplete="new-password" minLength={8} required />
          <small className="muted">{PASSWORD_RULE}</small>
        </label>
        <label>
          确认新密码
          <input value={confirm} onChange={(e) => setConfirm(e.target.value)} type="password" autoComplete="new-password" minLength={8} required />
        </label>
        {error && <p className="td-block">{error}</p>}
        {note && <p className="td-note">{note}</p>}
        <button className="td-submit buy" type="submit" disabled={busy}>
          {busy ? "提交中…" : "修改密码"}
        </button>
      </form>
    </section>
  );
}
