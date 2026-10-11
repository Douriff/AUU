import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { marketProvider } from "@/providers/HttpWsProvider";
import type { Trc20Admin, Trc20Transfer } from "@/types/contracts";
import { errText } from "@/i18n/errors";
import { bjTime, newIdemKey } from "@/pages/PointsPage/PointsPage";

function TxLink({ hash }: { hash: string }) {
  return (
    <a className="td-link" href={`https://tronscan.org/#/transaction/${hash}`} target="_blank" rel="noopener noreferrer" dir="ltr">
      {hash.slice(0, 10)}…{hash.slice(-6)}
    </a>
  );
}

/** Review-list action: attribute to a user (credit) or dismiss. Password + 2FA + reason every time. */
function ResolveForm({ tx, onDone }: { tx: Trc20Transfer; onDone: () => void }) {
  const { t } = useTranslation();
  const [mode, setMode] = useState<"resolve" | "dismiss">("resolve");
  const [user, setUser] = useState(tx.userId ?? "");
  const [points, setPoints] = useState("");
  const [reason, setReason] = useState("");
  const [pw, setPw] = useState("");
  const [code, setCode] = useState("");
  const [idem] = useState(newIdemKey);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState("");
  const submit = async () => {
    setBusy(true);
    setMsg("");
    try {
      if (mode === "resolve") {
        await marketProvider.adminTrc20Resolve({ tx_hash: tx.txHash, user_id: user.trim(), points: Number(points), reason: reason.trim(), password: pw, totp_code: code.trim(), idempotency_key: idem });
      } else {
        await marketProvider.adminTrc20Dismiss({ tx_hash: tx.txHash, reason: reason.trim(), password: pw, totp_code: code.trim() });
      }
      onDone();
    } catch (e) {
      setMsg(errText(e));
      setCode("");
    } finally {
      setBusy(false);
    }
  };
  const ok = reason.trim().length >= 2 && pw && code.trim() && (mode === "dismiss" || (user.trim() && /^\d+$/.test(points) && Number(points) > 0));
  return (
    <div className="auth-card usdt-resolve">
      <div className="usdt-presets">
        <button type="button" className={`td-chip${mode === "resolve" ? " is-active" : ""}`} onClick={() => setMode("resolve")}>{t("usdt.admin.resolve")}</button>
        <button type="button" className={`td-chip${mode === "dismiss" ? " is-active" : ""}`} onClick={() => setMode("dismiss")}>{t("usdt.admin.dismiss")}</button>
      </div>
      {mode === "resolve" ? (
        <>
          <label>{t("usdt.admin.userId")}<input value={user} onChange={(e) => setUser(e.target.value)} dir="ltr" maxLength={64} /></label>
          <label>{t("usdt.col.points")}<input value={points} onChange={(e) => setPoints(e.target.value.replace(/[^\d]/g, ""))} inputMode="numeric" dir="ltr" /></label>
        </>
      ) : null}
      <label>{t("points.admin.reason")}<input value={reason} onChange={(e) => setReason(e.target.value)} maxLength={200} /></label>
      <label>{t("sec.pwConfirm")}<input value={pw} onChange={(e) => setPw(e.target.value)} type="password" autoComplete="current-password" /></label>
      <label>{t("points.admin.code")}<input value={code} onChange={(e) => setCode(e.target.value)} inputMode="numeric" autoComplete="one-time-code" maxLength={16} /></label>
      <button type="button" className="td-submit buy" disabled={busy || !ok} onClick={() => void submit()}>
        {mode === "resolve" ? t("usdt.admin.resolveBtn") : t("usdt.admin.dismissBtn")}
      </button>
      {msg ? <p className="td-note">{msg}</p> : null}
    </div>
  );
}

export function Trc20AdminPanel() {
  const { t } = useTranslation();
  const [d, setD] = useState<Trc20Admin | null>(null);
  const [err, setErr] = useState("");
  const [open, setOpen] = useState<string | null>(null);
  const load = useCallback(() => {
    marketProvider.adminTrc20().then(setD).catch((e: unknown) => setErr(errText(e, "common.loadFailed")));
  }, []);
  useEffect(() => load(), [load]);
  if (err) return <div className="pro-alert">{err}</div>;
  if (!d) return null;
  const r = d.reconcile;
  return (
    <section className="settings-section usdt-admin">
      <h2>{t("usdt.admin.title")}</h2>
      {!d.config.enabled ? <div className="pro-alert is-warn">{t("usdt.admin.closed")}</div> : null}
      <p className="muted" dir="auto">
        {t("usdt.admin.address")}: <code dir="ltr">{d.config.address ?? "—"}</code> · {t("usdt.admin.addrNote")}
      </p>
      <p className="muted">{t("usdt.admin.params", { rate: d.config.rate, min: d.config.min, max: d.config.max, daily: d.config.dailyMax, pending: d.config.maxPending, ttl: d.config.ttlMin })}</p>
      <div className={`pro-alert ${r.balanced ? "is-ok" : ""}`}>
        <b>{t("usdt.admin.recon")}</b>{" "}
        <span dir="ltr">{t("usdt.admin.reconLine", { chain: r.chainTotal, n: r.chainCount, credited: r.creditedTotal, pending: r.pendingTotal, ignored: r.ignoredTotal })}</span>{" "}
        {r.balanced ? t("usdt.admin.reconOk") : t("usdt.admin.reconBad")}
        {r.fakeContractCount ? <> · {t("usdt.admin.fakeCount", { n: r.fakeContractCount })}</> : null}
        <br />
        <span className="muted">
          {r.lastPoll ? t("usdt.admin.lastPoll", { time: bjTime(r.lastPoll.at), source: r.lastPoll.source }) : t("usdt.admin.neverPolled")}
          {r.lastError ? ` · ${t("usdt.admin.lastError", { time: bjTime(r.lastError.at) })}` : ""}
        </span>
      </div>
      <h3>{t("usdt.admin.pending", { n: d.pending.length })}</h3>
      {d.pending.length ? (
        <div className="pro-table-wrap">
          <table className="pro-table usdt-table">
            <thead>
              <tr><th>{t("points.col.time")}</th><th>{t("usdt.col.amount")}</th><th>{t("usdt.admin.reason")}</th><th>{t("usdt.col.tx")}</th><th>{t("usdt.admin.order")}</th><th /></tr>
            </thead>
            <tbody>
              {d.pending.map((x) => (
                <tr key={x.txHash}>
                  <td>{bjTime(x.blockTs)}</td>
                  <td dir="ltr">{x.amount} {x.official ? "USDT" : <b className="down">{x.symbol} ⚠</b>}</td>
                  <td>{t(`usdt.reason.${x.reason ?? "amount_mismatch"}`)}</td>
                  <td><TxLink hash={x.txHash} /></td>
                  <td dir="ltr">{x.orderId ? `${x.orderId}${x.userName ? ` @${x.userName}` : ""}` : "—"}</td>
                  <td>
                    <button type="button" className="td-link" onClick={() => setOpen(open === x.txHash ? null : x.txHash)}>{t("usdt.admin.handle")}</button>
                    {open === x.txHash ? <ResolveForm tx={x} onDone={() => { setOpen(null); load(); }} /> : null}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <p className="muted">{t("usdt.admin.noPending")}</p>
      )}
      <h3>{t("usdt.admin.orders")}</h3>
      {d.orders.length ? (
        <div className="pro-table-wrap">
          <table className="pro-table usdt-table">
            <thead>
              <tr><th>{t("points.col.time")}</th><th>{t("points.admin.user")}</th><th>{t("usdt.col.amount")}</th><th>{t("usdt.col.points")}</th><th>{t("usdt.col.status")}</th><th>{t("usdt.col.tx")}</th></tr>
            </thead>
            <tbody>
              {d.orders.map((o) => (
                <tr key={o.orderId}>
                  <td>{bjTime(o.createdAt)}</td>
                  <td>{o.userName ? `@${o.userName}` : o.userId}</td>
                  <td dir="ltr">{o.amount}</td>
                  <td dir="ltr">{o.points.toLocaleString()}</td>
                  <td><span className={`td-badge usdt-st-${o.status}`}>{t(`usdt.status.${o.status}`)}</span></td>
                  <td>{o.txHash ? <TxLink hash={o.txHash} /> : "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <p className="muted">{t("usdt.noOrders")}</p>
      )}
    </section>
  );
}
