import { useCallback, useEffect, useState, type FormEvent } from "react";
import { marketProvider } from "@/providers/HttpWsProvider";
import type { AccountSecurity, TotpSetup } from "@/types/contracts";
import { useTranslation } from "react-i18next";
import i18n from "i18next";
import { errText } from "@/i18n/errors";
import { fmtDate } from "@/i18n/format";

/** Optional TOTP 2FA (off by default), recovery codes, login records and "sign out other sessions". */

const SH_OPTS = {
  timeZone: "Asia/Shanghai",
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  hour12: false,
} as const;
// i18n keys: sec.result.*, sec.method.*
const label = (group: string, k: string) => (i18n.exists(`sec.${group}.${k}`) ? i18n.t(`sec.${group}.${k}`) : k);

function shortUa(ua: string | null): string {
  if (!ua) return "—";
  const os = /iPhone|iPad|Android|Windows|Mac OS X|Linux/.exec(ua)?.[0] || "";
  const br = /Edg|Chrome|Firefox|Safari/.exec(ua)?.[0] || "";
  return [os.replace("Mac OS X", "macOS"), br === "Edg" ? "Edge" : br].filter(Boolean).join(" · ") || ua.slice(0, 40);
}

export function SecuritySection({ onChange }: { onChange?: () => void }) {
  const { t } = useTranslation();
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
      setError(errText(e, "sec.failed"));
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
      setNote(t("sec.enabledNote"));
      load();
      onChange?.();
    });
  };
  const reverify = (e: FormEvent) => {
    e.preventDefault();
    void run(async () => {
      if (mode === "disable") {
        await marketProvider.totpDisable(pw, code.trim());
        setNote(t("sec.disabledNote"));
        setCodes(null);
      } else {
        const r = await marketProvider.totpNewRecovery(pw, code.trim());
        setCodes(r.recovery_codes);
        setNote(t("sec.regeneratedNote"));
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
      await marketProvider.revokeOtherSessions();
      setNote(t("sec.revokedNote"));
      load();
    });

  if (!sec) return null;
  return (
    <div className="sec-wrap">
      <h2>{t("sec.title")}</h2>
      <div className="auth-card sec-card">
        <header>
          <h3>{t("sec.totp")}</h3>
          <span className={`td-badge ${sec.totp_enabled ? "paper" : ""}`}>{sec.totp_enabled ? t("sec.on") : t("sec.off")}</span>
        </header>
        {!sec.totp_enabled && !setup && (
          <form onSubmit={startSetup}>
            <p className="td-note">
              {t("sec.intro")}
            </p>
            <label>
              {t("sec.pwConfirm")}
              <input value={pw} onChange={(e) => setPw(e.target.value)} type="password" autoComplete="current-password" required />
            </label>
            <button className="td-submit buy" type="submit" disabled={busy}>
              {busy ? t("sec.generating") : t("sec.start")}
            </button>
          </form>
        )}
        {setup && (
          <form onSubmit={confirmSetup}>
            <p className="td-note">
              {t("sec.scan")}
            </p>
            <div className="sec-qr"><img className="sec-qr-img" src={setup.qr_png} alt={t("sec.qrAlt")} width={196} height={196} /></div>
            <code className="sec-secret">{setup.secret.replace(/(.{4})/g, "$1 ").trim()}</code>
            <label>
              {t("sec.code6")}
              <input value={code} onChange={(e) => setCode(e.target.value)} inputMode="numeric" autoComplete="one-time-code" maxLength={8} required />
            </label>
            <button className="td-submit buy" type="submit" disabled={busy}>
              {busy ? t("auth.login.verifying") : t("sec.verifyEnable")}
            </button>
            <button type="button" className="td-link" onClick={() => setSetup(null)}>
              {t("common.cancel")}
            </button>
          </form>
        )}
        {codes && (
          <div className="sec-codes">
            <p className="td-note">{t("sec.codesNote")}</p>
            <ol>
              {codes.map((c) => (
                <li key={c}>
                  <code>{c}</code>
                </li>
              ))}
            </ol>
            <button type="button" className="td-link" onClick={() => void navigator.clipboard?.writeText(codes.join("\n"))}>
              {t("sec.copyAll")}
            </button>
          </div>
        )}
        {sec.totp_enabled && !mode && (
          <div className="sec-actions">
            <span className="muted">{t("sec.left", { n: sec.recovery_left })}</span>
            <button type="button" className="td-link" onClick={() => setMode("recovery")}>
              {t("sec.regen")}
            </button>
            <button type="button" className="td-link danger" onClick={() => setMode("disable")}>
              {t("sec.disable")}
            </button>
          </div>
        )}
        {sec.totp_enabled && mode && (
          <form onSubmit={reverify}>
            <p className="td-note">{mode === "disable" ? t("sec.reverifyDisable") : t("sec.reverifyRegen")}</p>
            <label>
              {t("account.currentPw")}
              <input value={pw} onChange={(e) => setPw(e.target.value)} type="password" autoComplete="current-password" required />
            </label>
            <label>
              {t("auth.login.totpLabel")}
              <input value={code} onChange={(e) => setCode(e.target.value)} autoComplete="one-time-code" maxLength={16} required />
            </label>
            <button className="td-submit buy" type="submit" disabled={busy}>
              {busy ? t("common.submitting") : t("common.confirm")}
            </button>
            <button type="button" className="td-link" onClick={() => setMode("")}>
              {t("common.cancel")}
            </button>
          </form>
        )}
        {error && <p className="td-block">{error}</p>}
        {note && <p className="td-note">{note}</p>}
      </div>

      <div className="auth-card sec-card">
        <header>
          <h3>{t("sec.logins")}</h3>
          <button type="button" className="td-link danger" onClick={revoke} disabled={busy}>
            {t("sec.revoke")}
          </button>
        </header>
        <p className="td-note">{t("sec.sessionsNote")}</p>
        <div className="sec-log">
          <table>
            <thead>
              <tr>
                <th>{t("sec.colTime")}</th>
                <th>{t("sec.colResult")}</th>
                <th>IP</th>
                <th>{t("sec.colDevice")}</th>
              </tr>
            </thead>
            <tbody>
              {sec.logins.length ? (
                sec.logins.map((r) => (
                  <tr key={`${r.ts}-${r.result}`} className={r.result.startsWith("bad") ? "down" : ""}>
                    <td>{fmtDate(r.ts, SH_OPTS)}</td>
                    <td>
                      {label("result", r.result)}
                      {r.method && r.method !== "settings" ? <small className="muted"> · {label("method", r.method)}</small> : null}
                    </td>
                    <td>{r.ip || "—"}</td>
                    <td title={r.ua || ""}>{shortUa(r.ua)}</td>
                  </tr>
                ))
              ) : (
                <tr>
                  <td colSpan={4} className="muted">
                    {t("sec.noLogins")}
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
