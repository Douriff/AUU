import { FormEvent, useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { marketProvider } from "@/providers/HttpWsProvider";
import type { PointsFeatureConfig, WithdrawRequest } from "@/types/contracts";
import { errText } from "@/i18n/errors";
import { newIdemKey, bjTime } from "@/pages/PointsPage/PointsPage";

/** User withdrawal apply (default off). Admin pays USDT-TRC20 manually; private key never on server. */
export function WithdrawPanel({ onDone }: { onDone?: () => void }) {
  const { t } = useTranslation();
  const [cfg, setCfg] = useState<PointsFeatureConfig | null>(null);
  const [rows, setRows] = useState<WithdrawRequest[]>([]);
  const [usdt, setUsdt] = useState("");
  const [address, setAddress] = useState("");
  const [agree, setAgree] = useState(false);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState("");
  const [err, setErr] = useState("");
  const [idem, setIdem] = useState(newIdemKey);

  const load = useCallback(() => {
    marketProvider
      .getWithdraw()
      .then((r) => {
        setCfg(r.config);
        setRows(r.requests);
        setErr("");
      })
      .catch((e: unknown) => setErr(errText(e, "common.loadFailed")));
  }, []);
  useEffect(() => {
    load();
  }, [load]);

  if (cfg && !cfg.enabled) {
    return (
      <section className="settings-section" id="withdraw">
        <h2>{t("withdraw.title")}</h2>
        <div className="pro-alert is-warn">{t("withdraw.closed")}</div>
      </section>
    );
  }

  const n = /^\d+$/.test(usdt.trim()) ? Number(usdt.trim()) : NaN;
  const rate = cfg?.rate ?? 100;
  const lo = cfg?.min ?? 10;
  const hi = cfg?.max ?? 1000;
  const ok = Number.isInteger(n) && n >= lo && n <= hi && address.trim().length >= 30 && agree;

  const submit = async (ev: FormEvent) => {
    ev.preventDefault();
    if (!ok || busy) return;
    setBusy(true);
    setMsg("");
    try {
      const r = await marketProvider.applyWithdraw({ usdt: n, address: address.trim(), idempotency_key: idem });
      setMsg(t("withdraw.applied", { points: r.request.points, balance: r.balance }));
      setUsdt("");
      setAgree(false);
      setIdem(newIdemKey());
      load();
      onDone?.();
    } catch (e) {
      setMsg(errText(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="settings-section" id="withdraw">
      <h2>{t("withdraw.title")}</h2>
      <p className="muted">
        {t("withdraw.intro", { rate, min: lo, max: hi, daily: cfg?.dailyMax ?? 3000 })}
      </p>
      <p className="muted">{t("withdraw.manualNote")}</p>
      {err ? <div className="pro-alert">{err}</div> : null}
      <form className="pts-form" onSubmit={submit}>
        <label className="pt-field">
          <span>{t("withdraw.amountLabel", { min: lo, max: hi })}</span>
          <input inputMode="numeric" value={usdt} onChange={(e) => setUsdt(e.target.value)} maxLength={10} />
        </label>
        {Number.isInteger(n) && n >= lo && n <= hi ? (
          <p className="muted">{t("withdraw.preview", { usdt: n, points: n * rate })}</p>
        ) : null}
        <label className="pt-field">
          <span>{t("withdraw.address")}</span>
          <input value={address} onChange={(e) => setAddress(e.target.value)} placeholder="T…" maxLength={64} spellCheck={false} />
        </label>
        <label className="pts-agree">
          <input type="checkbox" checked={agree} onChange={(e) => setAgree(e.target.checked)} />
          <span>{t("withdraw.agree")}</span>
        </label>
        <button type="submit" className="td-submit" disabled={!ok || busy}>
          {busy ? t("common.submitting") : t("withdraw.apply")}
        </button>
      </form>
      {msg ? <p className="td-note">{msg}</p> : null}
      <h3>{t("withdraw.history")}</h3>
      {rows.length === 0 ? <p className="muted">{t("withdraw.noRequests")}</p> : (
        <div className="pro-table-wrap">
          <table className="pro-table pts-table">
            <thead>
              <tr>
                <th>{t("points.col.time")}</th>
                <th>{t("withdraw.col.usdt")}</th>
                <th>{t("points.col.amount")}</th>
                <th>{t("withdraw.col.status")}</th>
                <th>{t("withdraw.col.tx")}</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.id}>
                  <td>{bjTime(r.createdAt)}</td>
                  <td dir="ltr">{r.amountUsdt}</td>
                  <td dir="ltr">-{r.points}</td>
                  <td>{t(`withdraw.status.${r.status}`, { defaultValue: r.status })}</td>
                  <td>{r.txHash ? <a href={`https://tronscan.org/#/transaction/${r.txHash}`} target="_blank" rel="noreferrer"><code>{r.txHash.slice(0, 10)}…</code></a> : "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
