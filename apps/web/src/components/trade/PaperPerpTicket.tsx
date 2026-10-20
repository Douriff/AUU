import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { marketProvider } from "@/providers/HttpWsProvider";
import type { PaperAccount } from "@/types/mainstream";
import { errText } from "@/i18n/errors";
import { fmtDate, fmtFixed } from "@/i18n/format";

/** Paper USDT-M perpetual ticket: long/short, leverage (≤ server max, default 5), cross/isolated, market only. */

const ACK_KEY = "auu.paperPerpAck";
const n = (v: number, d = 2) => (Number.isFinite(v) ? fmtFixed(v, d) : "—");
const signed = (v: number, d = 2) => `${v > 0 ? "+" : ""}${n(v, d)}`;
const tone = (v: number) => (v > 0 ? "up" : v < 0 ? "down" : "");
const num = (s: string) => Number(s.trim().replace(",", "."));
const pxd = (px: number) => (px >= 10 ? 2 : px >= 1 ? 3 : px >= 0.01 ? 5 : 8);
function newId() {
  const c = globalThis.crypto as Crypto | undefined;
  return c?.randomUUID ? c.randomUUID() : `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 12)}`;
}

export function PaperPerpTicket({ symbol, price, acct, refresh }: { symbol: string; price: number; acct: PaperAccount; refresh: () => void }) {
  const { t } = useTranslation();
  const book = acct.perp;
  const maxLev = book?.maxLeverage ?? 5;
  const pos = book?.positions.find((p) => p.symbol === symbol);
  const [lev, setLev] = useState<number>(book?.settings.leverage ?? Math.min(3, maxLev));
  const [mm, setMm] = useState<"cross" | "isolated">(book?.settings.marginMode ?? "cross");
  const [amount, setAmount] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const pending = useRef<string | null>(null);

  useEffect(() => {
    if (pos) { setLev(pos.leverage); setMm(pos.marginMode); }
    else if (book?.settings.leverage) { setLev(book.settings.leverage); setMm(book.settings.marginMode ?? "cross"); }
  }, [symbol, pos?.leverage, pos?.marginMode, book?.settings.leverage, book?.settings.marginMode]); // eslint-disable-line react-hooks/exhaustive-deps

  const cost = acct.costs[symbol] ?? { takerFee: book?.takerFee ?? 0.0005, makerFee: 0.0002, slippage: 0.0003 };
  const fee = book?.takerFee ?? cost.takerFee;
  const mmr = book?.mmr ?? 0.005;
  const notional = num(amount) > 0 ? num(amount) : 0;
  const margin = notional / lev;
  const feeAmt = notional * fee;
  const lim = acct.limits;
  const liqLong = price > 0 ? (price * (1 - 1 / lev)) / (1 - mmr) : 0; // isolated estimate
  const liqShort = price > 0 ? (price * (1 + 1 / lev)) / (1 + mmr) : 0;
  const check =
    !(notional > 0) ? "" :
    notional < lim.minOrderUsdt ? t("trade.chk.min", { v: n(lim.minOrderUsdt, 0) }) :
    margin > lim.maxOrderUsdt ? t("trade.perp.chkMargin", { v: n(lim.maxOrderUsdt, 0) }) :
    margin + feeAmt > acct.availableCash ? t("trade.perp.chkCash") : "";

  const ensureAck = () => {
    if (localStorage.getItem(ACK_KEY) === "1") return true;
    if (!window.confirm(t("trade.perp.ack", { v: maxLev }))) return false;
    localStorage.setItem(ACK_KEY, "1");
    return true;
  };

  const submit = async (side: "buy" | "sell") => {
    if (busy || !(notional > 0) || check || !ensureAck()) return;
    setBusy(true);
    setMsg(null);
    pending.current = pending.current ?? newId();
    try {
      const o = await marketProvider.placePaperPerpOrder({
        client_order_id: pending.current, symbol, side, type: "market", market: "perp", notional, leverage: lev, margin_mode: mm, mode: "paper",
      });
      pending.current = null;
      setAmount("");
      setMsg({ ok: true, text: t("trade.perp.filled", { side: side === "buy" ? t("trade.perp.long") : t("trade.perp.short"), qty: n(o.qty, 6), sym: symbol, px: n(o.fillPrice ?? 0, pxd(o.fillPrice ?? 1)), fee: n(o.fee, 4) }) });
      refresh();
    } catch (e: unknown) {
      pending.current = null;
      setMsg({ ok: false, text: errText(e, "trade.orderFailed") });
    } finally {
      setBusy(false);
    }
  };

  const close = async () => {
    if (!pos || busy) return;
    setBusy(true);
    try {
      const r = await marketProvider.closePaperPerpPosition(symbol, `pclose-${newId()}`.slice(0, 64));
      setMsg({ ok: true, text: t("trade.perp.closed", { sym: symbol, pnl: signed(r.order.realized) }) });
      refresh();
    } catch (e: unknown) {
      setMsg({ ok: false, text: errText(e, "trade.orderFailed") });
    } finally {
      setBusy(false);
    }
  };

  const locked = Boolean(pos);
  const d = pxd(price || 1);
  return (
    <>
      <div className="pt-row pt-types" aria-label={t("trade.perp.marginMode")}>
        {(["cross", "isolated"] as const).map((m) => (
          <button key={m} type="button" disabled={locked} className={mm === m ? "is-on" : ""} onClick={() => setMm(m)}>
            {m === "cross" ? t("trade.perp.cross") : t("trade.perp.isolated")}
          </button>
        ))}
        <span className="muted pt-last num">{t("trade.last")} {price ? n(price, d) : "—"}</span>
      </div>
      <div className="pt-row pt-pcts" aria-label={t("trade.perp.leverage")}>
        {Array.from({ length: maxLev }, (_, i) => i + 1).map((x) => (
          <button key={x} type="button" disabled={locked} className={lev === x ? "is-on" : ""} onClick={() => setLev(x)}>{x}x</button>
        ))}
      </div>
      {locked ? <p className="muted pt-note">{t("trade.perp.lockedNote")}</p> : null}
      <label className="pt-field">
        <span>{t("trade.perp.notional")}</span>
        <input inputMode="decimal" placeholder="0" value={amount} onChange={(e) => setAmount(e.target.value)} />
      </label>
      <div className="pt-row pt-pcts">
        {[0.25, 0.5, 0.75, 1].map((p) => (
          <button key={p} type="button" onClick={() => {
            const room = Math.min(acct.availableCash / (1 / lev + fee), lim.maxOrderUsdt * lev, lim.maxPositionUsdt * lev);
            setAmount((room * p).toFixed(2));
          }}>{p * 100}%</button>
        ))}
      </div>
      <dl className="pt-est num">
        <div><dt>{t("ob.qty")}</dt><dd>{notional > 0 && price > 0 ? n(notional / price, 6) : "—"} {symbol}</dd></div>
        <div><dt>{t("trade.perp.margin")}</dt><dd>{notional > 0 ? `${n(margin)} USDT` : "—"}</dd></div>
        <div><dt>{t("trade.fee")} {fmtFixed(fee * 100, 3)}% <small className="muted">{t("trade.taker")}</small></dt><dd>{notional > 0 ? n(feeAmt, 4) : "—"}</dd></div>
        <div><dt>{t("trade.slippage")} {fmtFixed(cost.slippage * 1e4, 0)}bp</dt><dd>{notional > 0 ? n(notional * cost.slippage, 4) : "—"}</dd></div>
        <div><dt>{t("trade.perp.liqEst")}</dt><dd>{price > 0 ? `${n(liqLong, d)} / ${n(liqShort, d)}` : "—"}</dd></div>
        <div><dt>{t("trade.perp.funding")}</dt><dd>{n((book?.fundingRate ?? 0) * 100, 4)}% / 8h</dd></div>
      </dl>
      {check ? <p className="pt-warn">{check}</p> : null}
      <div className="pt-sides">
        <button type="button" className="pt-submit buy" disabled={busy || !(notional > 0) || !!check} onClick={() => submit("buy")}>{t("trade.perp.openLong", { lev })}</button>
        <button type="button" className="pt-submit sell" disabled={busy || !(notional > 0) || !!check} onClick={() => submit("sell")}>{t("trade.perp.openShort", { lev })}</button>
      </div>
      {msg ? <p className={msg.ok ? "pt-ok" : "pt-warn"}>{msg.text}</p> : null}
      <p className="muted pt-note">{t("trade.perp.limitsNote", { cash: n(acct.availableCash), order: n(lim.maxOrderUsdt, 0), lev: maxLev })}</p>

      <div className="pt-box">
        <div className="pt-box-head"><b>{t("trade.perp.position", { sym: symbol })}</b></div>
        {pos ? (
          <>
            <dl className="pt-est num">
              <div><dt>{t("trade.perp.side")}</dt><dd className={pos.side === "long" ? "up" : "down"}>{pos.side === "long" ? t("trade.perp.long") : t("trade.perp.short")} · {pos.leverage}x · {pos.marginMode === "cross" ? t("trade.perp.cross") : t("trade.perp.isolated")}</dd></div>
              <div><dt>{t("ob.qty")}</dt><dd>{n(pos.qty, 6)}</dd></div>
              <div><dt>{t("trade.perp.entryMark")}</dt><dd>{n(pos.entryPrice, d)} / {n(pos.last, d)}</dd></div>
              <div><dt>{t("trade.perp.margin")}</dt><dd>{n(pos.margin)} USDT</dd></div>
              <div><dt>{t("trade.perp.liq")}</dt><dd className="down">{n(pos.liqPrice, d)}</dd></div>
              <div><dt>{t("trade.upnl")}</dt><dd className={tone(pos.unrealized)}>{signed(pos.unrealized)} ({signed(pos.roe * 100)}%)</dd></div>
              <div><dt>{t("trade.perp.fundingPaid")}</dt><dd className={tone(pos.funding)}>{signed(pos.funding, 4)}</dd></div>
              <div><dt>{t("trade.realized")}</dt><dd className={tone(pos.realized)}>{signed(pos.realized)}</dd></div>
            </dl>
            <button type="button" className="pnl-close-btn" disabled={busy} onClick={close}>{t("trade.perp.close")}</button>
          </>
        ) : (
          <p className="muted pt-note">{t("trade.noPos")}</p>
        )}
        <dl className="pt-est num pt-acct">
          <div><dt>{t("trade.equity")}</dt><dd>{n(acct.equity)} USDT</dd></div>
          <div><dt>{t("trade.totalPnl")}</dt><dd className={tone(acct.pnl)}>{signed(acct.pnl)} ({signed((acct.pnlPct ?? 0) * 100)}%)</dd></div>
        </dl>
      </div>
      <div className="pt-box">
        <div className="pt-box-head"><b>{t("trade.history")}</b></div>
        <ul className="pt-orders num">
          {(book?.orders ?? []).slice(0, 20).map((o) => (
            <li key={o.id}>
              <span className={o.side === "buy" ? "up" : "down"}>{t(`trade.perp.kind.${o.kind}`)}·{o.side === "buy" ? t("chart.buy") : t("chart.sell")}·{o.leverage}x</span>
              <span>{n(o.qty, 6)} @ {n(o.fillPrice ?? 0, pxd(o.fillPrice ?? 1))}</span>
              <span className={tone(o.realized)}>{signed(o.realized)}</span>
              <span className="muted">{fmtDate(o.ts, { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false })}</span>
            </li>
          ))}
          {(book?.orders ?? []).length === 0 ? <li className="muted">{t("common.none")}</li> : null}
        </ul>
      </div>
    </>
  );
}
