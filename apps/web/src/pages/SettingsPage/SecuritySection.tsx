import { useCallback, useEffect, useState, type FormEvent } from "react";
import { marketProvider } from "@/providers/HttpWsProvider";
import type { AccountSecurity, TotpSetup } from "@/types/contracts";

/** Optional TOTP 2FA (off by default), recovery codes, login records and "sign out other sessions". */

const SH = new Intl.DateTimeFormat("zh-CN", {
  timeZone: "Asia/Shanghai",
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  hour12: false,
});
const RESULT: Record<string, string> = {
  ok: "登录成功",
  bad_password: "密码错误",
  bad_2fa: "两步验证码错误",
  password_ok_2fa_pending: "密码正确，等待两步验证",
  password_changed: "修改密码",
  "2fa_enabled": "开启两步验证",
  "2fa_disabled": "关闭两步验证",
  recovery_regenerated: "重新生成恢复码",
  revoked_other_sessions: "退出其他会话",
};
const METHOD: Record<string, string> = { password: "密码", totp: "验证器", recovery: "恢复码", settings: "设置" };

function shortUa(ua: string | null): string {
  if (!ua) return "—";
  const os = /iPhone|iPad|Android|Windows|Mac OS X|Linux/.exec(ua)?.[0] || "";
  const br = /Edg|Chrome|Firefox|Safari/.exec(ua)?.[0] || "";
  return [os.replace("Mac OS X", "macOS"), br === "Edg" ? "Edge" : br].filter(Boolean).join(" · ") || ua.slice(0, 40);
}

export function SecuritySection({ onChange }: { onChange?: () => void }) {
  const [sec, setSec] = useState<AccountSecurity | null>(null);
  const [setup, setSetup] = useState<TotpSetup | null>(null);
  const [codes, setCodes] = useState<string[] | null>(null);
  const [pw, setPw] = useState("");
  const [code, setCode] = useState("");
  const [mode, setMode] = useState<"" | "disable" | "recovery">("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [note, setNote] = useState("");

  const load = useCallback(() => {
    marketProvider
      .getSecurity()
      .then(setSec)
      .catch(() => setSec(null));
  }, []);
  useEffect(load, [load]);

  async function run(fn: () => Promise<void>) {
    setBusy(true);
    setError("");
    setNote("");
    try {
      await fn();
    } catch (e) {
      setError(e instanceof Error ? e.message : "操作失败");
    } finally {
      setBusy(false);
    }
  }

  const startSetup = (e: FormEvent) => {
    e.preventDefault();
    void run(async () => {
      setSetup(await marketProvider.totpSetup(pw));
      setPw("");
      setCode("");
    });
  };
  const confirmSetup = (e: FormEvent) => {
    e.preventDefault();
    void run(async () => {
      const r = await marketProvider.totpEnable(code.trim());
      setCodes(r.recovery_codes);
      setSetup(null);
      setCode("");
      setNote(r.message);
      load();
      onChange?.();
    });
  };
  const reverify = (e: FormEvent) => {
    e.preventDefault();
    void run(async () => {
      if (mode === "disable") {
        await marketProvider.totpDisable(pw, code.trim());
        setNote("两步验证已关闭");
        setCodes(null);
      } else {
        const r = await marketProvider.totpNewRecovery(pw, code.trim());
        setCodes(r.recovery_codes);
        setNote(r.message);
      }
      setMode("");
      setPw("");
      setCode("");
      load();
      onChange?.();
    });
  };
  const revoke = () =>
    void run(async () => {
      const r = await marketProvider.revokeOtherSessions();
      setNote(r.message);
      load();
    });

  if (!sec) return null;
  return (
    <div className="sec-wrap">
      <h2>账户安全</h2>
      <div className="auth-card sec-card">
        <header>
          <h3>两步验证（TOTP）</h3>
          <span className={`td-badge ${sec.totp_enabled ? "paper" : ""}`}>{sec.totp_enabled ? "已开启" : "未开启（可选）"}</span>
        </header>
        {!sec.totp_enabled && !setup && (
          <form onSubmit={startSetup}>
            <p className="td-note">
              开启后，登录时除密码外还要输入验证器 App（Google Authenticator、Microsoft Authenticator、1Password 等）里的 6 位数字。默认关闭。绑定二维码必须用验证器 App 扫，手机相机或微信扫不了。
            </p>
            <label>
              当前密码（确认是你本人）
              <input value={pw} onChange={(e) => setPw(e.target.value)} type="password" autoComplete="current-password" required />
            </label>
            <button className="td-submit buy" type="submit" disabled={busy}>
              {busy ? "生成中…" : "开始绑定"}
            </button>
          </form>
        )}
        {setup && (
          <form onSubmit={confirmSetup}>
            <p className="td-note">
              请用 Google Authenticator / Microsoft Authenticator 等验证器 App 扫码，手机相机或微信扫不了。也可手动输入密钥。扫进 App 后填入 6 位数字完成绑定，15 分钟内有效。
            </p>
            <div className="sec-qr" dangerouslySetInnerHTML={{ __html: setup.qr_svg }} aria-label="两步验证二维码" />
            <code className="sec-secret">{setup.secret.replace(/(.{4})/g, "$1 ").trim()}</code>
            <label>
              6 位验证码
              <input value={code} onChange={(e) => setCode(e.target.value)} inputMode="numeric" autoComplete="one-time-code" maxLength={8} required />
            </label>
            <button className="td-submit buy" type="submit" disabled={busy}>
              {busy ? "验证中…" : "验证并开启"}
            </button>
            <button type="button" className="td-link" onClick={() => setSetup(null)}>
              取消
            </button>
          </form>
        )}
        {codes && (
          <div className="sec-codes">
            <p className="td-note">恢复码（只显示这一次，请离线保存；每个只能用一次，手机丢失时用来登录）：</p>
            <ol>
              {codes.map((c) => (
                <li key={c}>
                  <code>{c}</code>
                </li>
              ))}
            </ol>
            <button type="button" className="td-link" onClick={() => void navigator.clipboard?.writeText(codes.join("\n"))}>
              复制全部
            </button>
          </div>
        )}
        {sec.totp_enabled && !mode && (
          <div className="sec-actions">
            <span className="muted">剩余恢复码 {sec.recovery_left} 个</span>
            <button type="button" className="td-link" onClick={() => setMode("recovery")}>
              重新生成恢复码
            </button>
            <button type="button" className="td-link danger" onClick={() => setMode("disable")}>
              关闭两步验证
            </button>
          </div>
        )}
        {sec.totp_enabled && mode && (
          <form onSubmit={reverify}>
            <p className="td-note">{mode === "disable" ? "关闭两步验证" : "重新生成恢复码（旧的全部作废）"}需要再次验证：</p>
            <label>
              当前密码
              <input value={pw} onChange={(e) => setPw(e.target.value)} type="password" autoComplete="current-password" required />
            </label>
            <label>
              两步验证码 / 恢复码
              <input value={code} onChange={(e) => setCode(e.target.value)} autoComplete="one-time-code" maxLength={16} required />
            </label>
            <button className="td-submit buy" type="submit" disabled={busy}>
              {busy ? "提交中…" : "确认"}
            </button>
            <button type="button" className="td-link" onClick={() => setMode("")}>
              取消
            </button>
          </form>
        )}
        {error && <p className="td-block">{error}</p>}
        {note && <p className="td-note">{note}</p>}
      </div>

      <div className="auth-card sec-card">
        <header>
          <h3>登录记录</h3>
          <button type="button" className="td-link danger" onClick={revoke} disabled={busy}>
            退出其他会话
          </button>
        </header>
        <p className="td-note">{sec.sessions_note}</p>
        <div className="sec-log">
          <table>
            <thead>
              <tr>
                <th>时间（北京）</th>
                <th>结果</th>
                <th>IP</th>
                <th>设备</th>
              </tr>
            </thead>
            <tbody>
              {sec.logins.length ? (
                sec.logins.map((r) => (
                  <tr key={`${r.ts}-${r.result}`} className={r.result.startsWith("bad") ? "down" : ""}>
                    <td>{SH.format(new Date(r.ts))}</td>
                    <td>
                      {RESULT[r.result] || r.result}
                      {r.method && r.method !== "settings" ? <small className="muted"> · {METHOD[r.method] || r.method}</small> : null}
                    </td>
                    <td>{r.ip || "—"}</td>
                    <td title={r.ua || ""}>{shortUa(r.ua)}</td>
                  </tr>
                ))
              ) : (
                <tr>
                  <td colSpan={4} className="muted">
                    暂无记录（从这个版本开始记录）
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
