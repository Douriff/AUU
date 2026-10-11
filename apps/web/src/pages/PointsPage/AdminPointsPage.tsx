import { FormEvent, useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { marketProvider } from "@/providers/HttpWsProvider";
import type { PointsAccount, PointsUserRow } from "@/types/contracts";
import { errText } from "@/i18n/errors";
import { EntryTable, newIdemKey } from "@/pages/PointsPage/PointsPage";

type UserView = PointsAccount & { user: { id: string; name: string; display_name: string } };

/** Admin: look up balances, adjust with password + 2FA and a required reason, export CSV, reconcile. */
export function AdminPointsPage() {
  const { t } = useTranslation();
  const [q, setQ] = useState("");
  const [rows, setRows] = useState<PointsUserRow[] | null>(null);
  const [sel, setSel] = useState<UserView | null>(null);
  const [err, setErr] = useState("");
  const [verify, setVerify] = useState("");
  // adjust form
  const [amount, setAmount] = useState("");
  const [reason, setReason] = useState("");
  const [pw, setPw] = useState("");
  const [code, setCode] = useState("");
  const [confirm, setConfirm] = useState(false);
  const [idem, setIdem] = useState(newIdemKey);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState("");

  const search = useCallback((needle: string) => {
    marketProvider.adminPointsUsers(needle).then((r) => setRows(r.items)).catch((e: unknown) => setErr(errText(e, "common.loadFailed")));
  }, []);
  useEffect(() => search(""), [search]);

  const open = (id: string) => {
    setMsg("");
    setConfirm(false);
    marketProvider.adminPointsUser(id).then(setSel).catch((e: unknown) => setErr(errText(e, "common.loadFailed")));
  };

  const n = /^-?\d+$/.test(amount.trim()) ? Number(amount.trim()) : NaN;
  const formOk = Number.isInteger(n) && n !== 0 && Math.abs(n) <= 1_000_000 && reason.trim().length >= 2;

  const submit = async (ev: FormEvent) => {
    ev.preventDefault();
    if (!sel || !formOk) return;
    if (!confirm) {
      setConfirm(true);
      return;
    }
    setBusy(true);
    setMsg("");
    try {
      const r = await marketProvider.adminPointsAdjust({ user_id: sel.user.id, amount: n, reason: reason.trim(), password: pw, totp_code: code.trim(), idempotency_key: idem });
      setMsg(t("points.admin.done", { balance: r.balance }));
      setAmount("");
      setReason("");
      setPw("");
      setCode("");
      setConfirm(false);
      setIdem(newIdemKey());
      open(sel.user.id);
      search(q);
    } catch (e) {
      setMsg(errText(e));
      setCode("");
    } finally {
      setBusy(false);
    }
  };

  const runVerify = () => {
    marketProvider
      .adminPointsVerify()
      .then((v) => setVerify(v.ok ? t("points.admin.verifyOk", { n: v.entries, users: v.users }) : t("points.admin.verifyBad", { list: v.problems.join("; ") })))
      .catch((e: unknown) => setVerify(errText(e)));
  };

  return (
    <div className="shell-page settings-page pro-page points-admin">
      <header className="pro-head">
        <h1>{t("points.admin.title")}</h1>
        <p>{t("points.admin.sub")}</p>
      </header>
      {err ? <div className="pro-alert">{err}</div> : null}
      <section className="settings-section">
        <div className="set-row">
          <form
            className="pts-search"
            onSubmit={(e) => {
              e.preventDefault();
              search(q);
            }}
          >
            <input value={q} onChange={(e) => setQ(e.target.value)} placeholder={t("points.admin.search")} maxLength={64} />
            <button type="submit" className="td-link">{t("points.admin.searchBtn")}</button>
          </form>
          <div className="pts-tools">
            <a className="td-link" href={marketProvider.adminPointsExportUrl()} download>{t("points.admin.exportAll")}</a>
            <button type="button" className="td-link" onClick={runVerify}>{t("points.admin.verify")}</button>
            <Link className="td-link" to="/points">{t("points.title")} ›</Link>
          </div>
        </div>
        {verify ? <p className="td-note">{verify}</p> : null}
        <div className="pro-table-wrap">
          <table className="pro-table">
            <thead>
              <tr>
                <th>{t("points.admin.user")}</th>
                <th>{t("points.balance")}</th>
              </tr>
            </thead>
            <tbody>
              {(rows ?? []).map((u) => (
                <tr key={u.id} className={`is-link${sel?.user.id === u.id ? " is-active" : ""}`} onClick={() => open(u.id)}>
                  <td>{u.display_name || u.name} <span className="muted">@{u.name}</span></td>
                  <td dir="ltr">{u.balance}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>
      {sel ? (
        <section className="settings-section">
          <h2>
            {sel.user.display_name || sel.user.name} · {t("points.balance")} <b dir="ltr">{sel.balance}</b>
          </h2>
          <a className="td-link" href={marketProvider.adminPointsExportUrl(sel.user.id)} download>{t("points.admin.exportUser")}</a>
          <form className="auth-card pts-adjust" onSubmit={(e) => void submit(e)}>
            <h3>{t("points.admin.adjust")}</h3>
            <p className="td-note">{t("points.admin.adjustNote")}</p>
            <label>
              {t("points.admin.amount")}
              <input value={amount} onChange={(e) => { setAmount(e.target.value); setConfirm(false); }} inputMode="numeric" placeholder="+100 / -50" required dir="ltr" />
            </label>
            <label>
              {t("points.admin.reason")}
              <input value={reason} onChange={(e) => { setReason(e.target.value); setConfirm(false); }} maxLength={200} required />
            </label>
            <label>
              {t("sec.pwConfirm")}
              <input value={pw} onChange={(e) => setPw(e.target.value)} type="password" autoComplete="current-password" required />
            </label>
            <label>
              {t("points.admin.code")}
              <input value={code} onChange={(e) => setCode(e.target.value)} inputMode="numeric" autoComplete="one-time-code" maxLength={16} required />
            </label>
            {confirm ? (
              <div className="pro-alert is-warn">{t("points.admin.confirm", { name: sel.user.name, n: n > 0 ? `+${n}` : n, after: sel.balance + n })}</div>
            ) : null}
            <button className="td-submit buy" type="submit" disabled={busy || !formOk}>
              {confirm ? t("points.admin.confirmBtn") : t("points.admin.next")}
            </button>
            {confirm ? (
              <button type="button" className="td-link" onClick={() => setConfirm(false)}>{t("common.cancel")}</button>
            ) : null}
            {msg ? <p className="td-note">{msg}</p> : null}
          </form>
          <h3>{t("points.history")}</h3>
          <EntryTable entries={sel.entries} admin />
        </section>
      ) : null}
    </div>
  );
}
