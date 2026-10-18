import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { ApiError, marketProvider } from "@/providers/HttpWsProvider";
import type { PaperAccount, PaperCloseResult } from "@/types/mainstream";
import type { PnlSummary } from "@/types/contracts";
import { errText } from "@/i18n/errors";
import { fmtDate } from "@/i18n/format";
import { newClientId } from "@/lib/clientId";
import { money, signed } from "@/pages/PnlPage/fmt";
import { tone } from "@/pages/PnlPage/usePnl";

export type CloseNote = { kind: "ok" | "err"; text: string } | null;

const fmtQty = (v: number) => v.toLocaleString(undefined, { maximumFractionDigits: v >= 1 ? 4 : 8 });
const fmtPx = (v: number) => v.toLocaleString(undefined, { maximumFractionDigits: v >= 1000 ? 2 : v >= 1 ? 4 : 6 });

/** Paper account (ledger + costs) next to the P&L summary; reload() is called right after every close. */
export function usePaperAccount() {
  const [acct, setAcct] = useState<PaperAccount | null>(null);
  const load = useCallback(() => marketProvider.getPaperAccount().then(setAcct).catch(() => undefined), []);
  useEffect(() => {
    load();
    const id = window.setInterval(() => !document.hidden && load(), 15_000);
    return () => window.clearInterval(id);
  }, [load]);
  return { acct, reload: load };
}

/**
 * Close flow state. One client id per user intent: it is kept across network failures (so a retry cannot
 * close twice) and dropped once the server answered. A synchronous ref blocks double clicks before React re-renders.
 */
export function usePaperClose(afterClose: () => void) {
  const { t } = useTranslation();
  const [busy, setBusy] = useState<string | null>(null);
  const [note, setNote] = useState<CloseNote>(null);
  const lock = useRef(false);
  const ids = useRef<Record<string, string>>({});

  const run = useCallback(async (key: string, call: (cid: string) => Promise<PaperCloseResult>, onOk: (r: PaperCloseResult) => CloseNote) => {
    if (lock.current) return false;
    lock.current = true;
    setBusy(key);
    setNote(null);
    const cid = (ids.current[key] ??= newClientId("pc", 22));
    try {
      const r = await call(cid);
      delete ids.current[key];
      setNote(onOk(r));
      return true;
    } catch (e) {
      if (e instanceof ApiError) delete ids.current[key]; // the server answered: a new attempt is a new intent
      setNote({ kind: "err", text: errText(e, "common.requestFailed") });
      return false;
    } finally {
      lock.current = false;
      setBusy(null);
      afterClose(); // refresh positions, realized P&L and the ledger either way
    }
  }, [afterClose]);

  const closeOne = useCallback((symbol: string) => run(
    `c:${symbol}`,
    (cid) => marketProvider.closePaperPosition(symbol, cid),
    (r) => {
      const o = r.order ?? r.closed[0];
      if (!o) return null;
      if (o.duplicate) return { kind: "ok", text: t("pnl.close.dup", { symbol }) };
      return { kind: "ok", text: t("pnl.close.done", { symbol, qty: fmtQty(o.qty), price: fmtPx(o.fillPrice ?? 0), pnl: signed(o.realized) }) };
    },
  ), [run, t]);

  const closeAll = useCallback(() => run(
    "all",
    (cid) => marketProvider.closeAllPaperPositions(cid),
    (r) => {
      if (r.duplicate) return { kind: "ok", text: t("pnl.close.dupAll") };
      if (r.failed.length) {
        return { kind: "err", text: t("pnl.close.doneAllPartial", { n: r.closed.length, m: r.failed.length, symbols: r.failed.map((f) => f.symbol).join(", ") }) };
      }
      return { kind: "ok", text: t("pnl.close.doneAll", { n: r.closed.length, pnl: signed(r.realized) }) };
    },
  ), [run, t]);

  return { busy, note, setNote, closeOne, closeAll };
}

/** Second confirmation for "close all": lists what will be sold and the estimated proceeds. */
export function CloseAllModal({ s, acct, busy, onConfirm, onCancel }: {
  s: PnlSummary; acct: PaperAccount | null; busy: boolean; onConfirm: () => void; onCancel: () => void;
}) {
  const { t } = useTranslation();
  let value = 0;
  let fee = 0;
  for (const p of s.positions) {
    const c = acct?.costs?.[p.symbol];
    const net = p.value * (1 - (c?.slippage ?? 0));
    value += net;
    fee += net * (c?.takerFee ?? 0);
  }
  return (
    <div className="modal-backdrop" role="presentation">
      <div className="modal-card close-modal" role="dialog" aria-modal="true" aria-label={t("pnl.close.confirmTitle")}>
        <h2>{t("pnl.close.confirmTitle")}</h2>
        <p>{t("pnl.close.confirmBody", { n: s.positions.length })}</p>
        <table className="pro-table close-modal-table">
          <thead><tr><th>{t("pnl.col.symbol")}</th><th>{t("pnl.col.qty")}</th><th>{t("pnl.col.last")}</th><th>{t("pnl.col.pnl")}</th></tr></thead>
          <tbody>
            {s.positions.map((p) => (
              <tr key={p.symbol}>
                <td>{p.symbol}</td><td className="num">{fmtQty(p.qty)}</td><td className="num">{fmtPx(p.last)}</td>
                <td className={`num ${tone(p.unrealized)}`}>{signed(p.unrealized)}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <p className="td-note">{t("pnl.close.confirmEst", { value: money(value), fee: money(fee) })}</p>
        <div className="close-modal-actions">
          <button type="button" className="td-link" disabled={busy} onClick={onCancel}>{t("pnl.close.cancel")}</button>
          <button type="button" className="td-submit close-go" disabled={busy} onClick={onConfirm}>
            {busy ? t("pnl.close.busy") : t("pnl.close.confirmGo")}
          </button>
        </div>
      </div>
    </div>
  );
}

/** Recent fills of the paper ledger (includes the closes). */
export function PaperLedger({ acct }: { acct: PaperAccount | null }) {
  const { t } = useTranslation();
  const rows = (acct?.orders ?? []).filter((o) => o.status === "filled").slice(0, 12);
  return (
    <section className="pnl-section pane">
      <div className="pnl-sec-head">
        <h2>{t("pnl.ledger.title")}</h2>
        <span className="muted">{t("pnl.ledger.sub")}</span>
      </div>
      {rows.length === 0 ? <p className="muted">{t("pnl.ledger.empty")}</p> : (
        <div className="pro-table-wrap">
          <table className="pro-table pnl-table pnl-ledger">
            <thead>
              <tr>
                <th>{t("pnl.ledger.time")}</th><th>{t("pnl.col.symbol")}</th><th>{t("pnl.ledger.side")}</th><th>{t("pnl.col.qty")}</th>
                <th>{t("pnl.ledger.price")}</th><th>{t("pnl.ledger.fee")}</th><th>{t("pnl.ledger.realized")}</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((o) => (
                <tr key={o.id} className={o.reason === "CLOSE" ? "is-close" : ""}>
                  <td className="num">{fmtDate(o.fillTs ?? o.ts, { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false })}</td>
                  <td>{o.symbol}</td>
                  <td>
                    {o.side === "buy" ? t("pnl.ledger.buy") : t("pnl.ledger.sell")}
                    {o.reason === "CLOSE" ? <span className="pnl-close-tag">{t("pnl.ledger.closeTag")}</span> : null}
                  </td>
                  <td className="num">{fmtQty(o.qty)}</td>
                  <td className="num">{fmtPx(o.fillPrice ?? 0)}</td>
                  <td className="num">{money(o.fee)}</td>
                  <td className={`num ${tone(o.side === "sell" ? o.realized : 0)}`}>{o.side === "sell" ? signed(o.realized) : "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
