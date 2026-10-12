import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { marketProvider } from "@/providers/HttpWsProvider";
import type { AuthMe, PointsAccount, PointsEntry } from "@/types/contracts";
import { errText } from "@/i18n/errors";
import { fmtDate } from "@/i18n/format";
import { Empty, Sk } from "@/components/ui/Skeleton";
import { Trc20Topup } from "@/pages/PointsPage/Trc20Topup";
import { WithdrawPanel } from "@/pages/PointsPage/WithdrawPanel";

/** 积分: balance, append-only history, top-up (USDT-TRC20 when the server has a receive address), example unlock. */

export function newIdemKey(): string {
  const c = globalThis.crypto;
  if (c?.randomUUID) return c.randomUUID();
  const b = new Uint8Array(18);
  c.getRandomValues(b);
  return Array.from(b, (x) => x.toString(16).padStart(2, "0")).join("");
}

export function bjTime(ms: number) {
  return fmtDate(ms, { timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false });
}

/** What a ledger row says, in the UI language (admin free-text reasons are shown as written). */
export function EntryReason({ e }: { e: PointsEntry }) {
  const { t } = useTranslation();
  const ref = e.ref ?? "";
  if (e.kind === "spend" && ref.startsWith("item:")) return <>{t("points.unlock", { item: t(`points.item.${ref.slice(5)}.name`, { defaultValue: ref.slice(5) }) })}</>;
  if (e.kind === "topup" && e.operatorKind === "provider") return <>{t("points.viaProvider", { p: t(`points.provider.${e.provider}`, { defaultValue: e.provider ?? "" }) })}</>;
  return <span dir="auto">{e.reason}</span>;
}

export function EntryTable({ entries, admin = false }: { entries: PointsEntry[]; admin?: boolean }) {
  const { t } = useTranslation();
  if (!entries.length) return <Empty title={t("points.noEntries")} />;
  return (
    <div className="pro-table-wrap">
      <table className="pro-table pts-table">
        <thead>
          <tr>
            <th>{t("points.col.time")}</th>
            <th>{t("points.col.kind")}</th>
            <th>{t("points.col.amount")}</th>
            <th>{t("points.col.balance")}</th>
            <th className="pts-reason">{t("points.col.reason")}</th>
            <th>{t("points.col.operator")}</th>
            {admin ? <th>#</th> : null}
          </tr>
        </thead>
        <tbody>
          {entries.map((e) => (
            <tr key={e.id}>
              <td>{bjTime(e.ts)}</td>
              <td>{t(`points.kind.${e.kind}`)}</td>
              <td className={e.amount > 0 ? "up" : "down"} dir="ltr">{e.amount > 0 ? `+${e.amount}` : e.amount}</td>
              <td dir="ltr">{e.balance_after}</td>
              <td className="pts-reason"><EntryReason e={e} /></td>
              <td>{admin && e.operator ? <code>{e.operator}</code> : t(`points.op.${e.operatorKind}`, { defaultValue: e.operatorKind })}</td>
              {admin ? <td dir="ltr">{e.id}</td> : null}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function PointsPage() {
  const { t } = useTranslation();
  const [me, setMe] = useState<AuthMe | null>(null);
  const [acct, setAcct] = useState<PointsAccount | null>(null);
  const [err, setErr] = useState("");
  const [msg, setMsg] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(() => {
    marketProvider.getPointsMe().then(setAcct).catch((e: unknown) => setErr(errText(e, "common.loadFailed")));
  }, []);
  useEffect(() => {
    marketProvider.getMe().then(setMe).catch(() => undefined);
    load();
  }, [load]);

  const unlock = async (item: string) => {
    setBusy(true);
    setMsg("");
    try {
      await marketProvider.pointsSpend(item, newIdemKey());
      setMsg(t("points.unlocked"));
      load();
    } catch (e) {
      setMsg(errText(e));
    } finally {
      setBusy(false);
    }
  };

  const topupOpen = Boolean(acct?.topup?.open);
  return (
    <div className="shell-page settings-page pro-page points-page">
      <header className="pro-head">
        <h1>{t("points.title")}</h1>
        <p>{t("points.sub")}</p>
      </header>
      {err ? <div className="pro-alert">{err}</div> : null}
      <section className="settings-section pts-balance">
        <div className="pts-big">
          <span className="muted">{t("points.balance")}</span>
          <b dir="ltr">{acct ? acct.balance.toLocaleString() : <Sk w={80} h={24} />}</b>
        </div>
        <ul className="pts-rules">
          <li>{t("points.rule.inApp")}</li>
          <li>{acct?.withdraw?.enabled ? t("points.rule.withdrawOn") : t("points.rule.noWithdraw")}</li>
          <li>{t("points.rule.separate")}</li>
          <li>{acct?.pointsTrading?.enabled ? t("points.rule.tradingOn") : t("points.rule.tradingOff")}</li>
        </ul>
        {me?.user?.is_admin ? (
          <Link className="td-link" to="/admin">{t("admin.consoleLink")} ›</Link>
        ) : null}
      </section>
      <section className="settings-section" id="topup">
        <h2>{t("points.topup")}</h2>
        {acct && !topupOpen ? <div className="pro-alert is-warn pts-closed">{t("points.topupClosed")}</div> : null}
        {acct && topupOpen && acct.topup?.channels.includes("usdt_trc20") ? <Trc20Topup /> : null}
        {acct && topupOpen && !acct.topup?.channels.includes("usdt_trc20") ? <p className="muted">{t("points.topupChannels", { list: acct.topup?.channels.join(", ") })}</p> : null}
        <p className="muted">{t("points.topupNote")}</p>
      </section>
      {acct ? <WithdrawPanel onDone={load} /> : null}
      <section className="settings-section">
        <h2>{t("points.unlocks")}</h2>
        <p className="muted">{t("points.unlocksNote")}</p>
        {acct?.shop.items.map((it) => {
          const ent = acct.entitlements[it.id];
          return (
            <div className="set-row pts-item" key={it.id}>
              <div>
                <b>{t(`points.item.${it.id}.name`)}</b>
                {it.example ? <span className="td-badge"> {t("points.example")}</span> : null}
                <p className="muted">{t(`points.item.${it.id}.desc`)}</p>
                <p className="muted">
                  {t("points.cost", { n: it.cost })} · {it.days ? t("points.days", { n: it.days }) : t("points.permanent")}
                  {ent?.active && ent.until ? ` · ${t("points.activeUntil", { time: bjTime(ent.until) })}` : ""}
                </p>
              </div>
              <button type="button" className="td-submit buy" disabled={busy || !acct.shop.enabled} onClick={() => void unlock(it.id)}>
                {acct.shop.enabled ? t("points.unlockBtn") : t("points.shopOff")}
              </button>
            </div>
          );
        })}
        {msg ? <p className="td-note">{msg}</p> : null}
      </section>
      <section className="settings-section">
        <h2>{t("points.history")}</h2>
        {acct ? <EntryTable entries={acct.entries} /> : <Sk w={240} h={12} />}
      </section>
    </div>
  );
}
