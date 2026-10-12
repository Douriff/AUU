import { FormEvent, useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { marketProvider } from "@/providers/HttpWsProvider";
import type { PointsFeatureConfig, WithdrawRequest } from "@/types/contracts";
import { errText } from "@/i18n/errors";
import { bjTime } from "@/pages/PointsPage/PointsPage";

/** Admin: review pending withdrawals, reject (unfreeze) or register TokenPocket tx hash + on-chain verify. */
export function AdminWithdrawPanel() {
  const { t } = useTranslation();
  const [cfg, setCfg] = useState<PointsFeatureConfig | null>(null);
  const [items, setItems] = useState<WithdrawRequest[]>([]);
  const [err, setErr] = useState("");
  const [sel, setSel] = useState<WithdrawRequest | null>(null);
  const [tx, setTx] = useState("");
  const [reason, setReason] = useState("");
  const [pw, setPw] = useState("");
  const [code, setCode] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState("");

  const load = useCallback(() => {
    marketProvider
      .adminWithdrawals()
      .then((r) => {
        setCfg(r.config);
        setItems(r.items);
      })
      .catch((e: unknown) => setErr(errText(e, "common.loadFailed")));
  }, []);
  useEffect(() => {
    load();
  }, [load]);

  const act = async (ev: FormEvent, kind: "reject" | "complete") => {
    ev.preventDefault();
    if (!sel || busy || reason.trim().length < 2) return;
    setBusy(true);
    setMsg("");
    try {
      if (kind === "reject") {
        await marketProvider.adminWithdrawReject({
          request_id: sel.id,
          reason: reason.trim(),
          password: pw,
          totp_code: code.trim(),
        });
      } else {
        await marketProvider.adminWithdrawComplete({
          request_id: sel.id,
          tx_hash: tx.trim(),
          reason: reason.trim(),
          password: pw,
          totp_code: code.trim(),
        });
      }
      setMsg(t("withdraw.admin.done"));
      setSel(null);
      setTx("");
      setReason("");
      setPw("");
      setCode("");
      load();
    } catch (e) {
      setMsg(errText(e));
      setCode("");
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="settings-section">
      <h2>{t("withdraw.admin.title")}</h2>
      <p className="muted">{t("withdraw.admin.sub")}</p>
      {cfg ? (
        <p className="muted">
          {t("withdraw.admin.flags", {
            on: cfg.enabled ? t("common.on") : t("common.off"),
            adminOnly: cfg.adminOnly ? t("common.on") : t("common.off"),
            rate: cfg.rate,
            min: cfg.min,
            max: cfg.max,
          })}
        </p>
      ) : null}
      {err ? <div className="pro-alert">{err}</div> : null}
      <div className="pro-table-wrap">
        <table className="pro-table">
          <thead>
            <tr>
              <th>{t("points.col.time")}</th>
              <th>{t("points.admin.user")}</th>
              <th>USDT</th>
              <th>{t("points.balance")}</th>
              <th>{t("withdraw.col.status")}</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {items.map((r) => (
              <tr key={r.id} className={sel?.id === r.id ? "is-active" : ""}>
                <td>{bjTime(r.createdAt)}</td>
                <td>{r.userName || r.userId}</td>
                <td dir="ltr">{r.amountUsdt}</td>
                <td dir="ltr">{r.points}</td>
                <td>{t(`withdraw.status.${r.status}`, { defaultValue: r.status })}</td>
                <td>
                  {r.status === "pending" ? (
                    <button type="button" className="td-link" onClick={() => { setSel(r); setMsg(""); }}>{t("withdraw.admin.handle")}</button>
                  ) : r.txHash ? (
                    <a href={`https://tronscan.org/#/transaction/${r.txHash}`} target="_blank" rel="noreferrer"><code>{r.txHash.slice(0, 8)}…</code></a>
                  ) : null}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {sel ? (
        <form className="auth-card pts-adjust" onSubmit={(e) => void act(e, "complete")}>
          <h3>{t("withdraw.admin.handle")} · {sel.amountUsdt} USDT → {sel.address}</h3>
          <p className="muted">{t("withdraw.admin.payHint")}</p>
          <label>
            {t("withdraw.admin.txHash")}
            <input value={tx} onChange={(e) => setTx(e.target.value)} spellCheck={false} maxLength={80} placeholder="64 hex" />
          </label>
          <label>
            {t("points.admin.reason")}
            <input value={reason} onChange={(e) => setReason(e.target.value)} maxLength={200} required />
          </label>
          <label>
            {t("sec.pwConfirm")}
            <input value={pw} onChange={(e) => setPw(e.target.value)} type="password" autoComplete="current-password" required />
          </label>
          <label>
            {t("points.admin.code")}
            <input value={code} onChange={(e) => setCode(e.target.value)} inputMode="numeric" maxLength={16} required />
          </label>
          <div className="pts-tools">
            <button className="td-submit buy" type="submit" disabled={busy || tx.trim().length < 64}>{t("withdraw.admin.completeBtn")}</button>
            <button
              className="td-submit"
              type="button"
              disabled={busy}
              onClick={(e) => void act(e as unknown as FormEvent, "reject")}
            >
              {t("withdraw.admin.rejectBtn")}
            </button>
            <button type="button" className="td-link" onClick={() => setSel(null)}>{t("common.cancel")}</button>
          </div>
          {msg ? <p className="td-note">{msg}</p> : null}
        </form>
      ) : null}
    </section>
  );
}
