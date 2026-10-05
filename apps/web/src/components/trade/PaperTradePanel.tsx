import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { marketProvider } from "@/providers/HttpWsProvider";
import type { PaperAccount, PaperOrder } from "@/types/mainstream";
import { useTranslation } from "react-i18next";
import i18n from "i18next";
import { errText } from "@/i18n/errors";
import { fmtDate, fmtFixed } from "@/i18n/format";

/** Paper-only order ticket + position + orders for one coin. Live trading stays locked server-side. */

type Side = "buy" | "sell";
type OType = "market" | "limit" | "tpsl";

const POLL_MS = 5_000;

function n(v: number, d = 2) {
  return Number.isFinite(v) ? fmtFixed(v, d) : "—";
}
function pxDigits(px: number) {
  return px >= 1000 ? 2 : px >= 10 ? 2 : px >= 1 ? 3 : px >= 0.01 ? 5 : Math.min(10, Math.ceil(-Math.log10(px)) + 3); // display only; sub-cent 大盘 coins
}
function signed(v: number, d = 2) {
  return `${v > 0 ? "+" : ""}${n(v, d)}`;
}
function tone(v: number) {
  return v > 0 ? "up" : v < 0 ? "down" : "";
}
function hhmm(ms: number) {
  return fmtDate(ms, { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
}
/** Accept "1,5" as well as "1.5" (comma-decimal languages). */
function num(s: string) {
  return Number(s.trim().replace(",", "."));
}
function newId() {
  const c = globalThis.crypto as Crypto | undefined;
  return c?.randomUUID ? c.randomUUID() : `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 12)}`;
}

// i18n keys: trade.status.*, trade.type.*, trade.reason.*
const REASONS = new Set(["OCO_CANCEL", "TP_TRIGGERED", "SL_TRIGGERED", "INSUFFICIENT_POSITION"]);
const typeLabel = (t: string) => (i18n.exists(`trade.type.${t}`) ? i18n.t(`trade.type.${t}`) : t);

export function PaperTradePanel({ symbol, price }: { symbol: string; price: number | null | undefined }) {
  const { t } = useTranslation();
  const [acct, setAcct] = useState<PaperAccount | null>(null);
  const [loadErr, setLoadErr] = useState("");
  const [side, setSide] = useState<Side>("buy");
  const [otype, setOtype] = useState<OType>("market");
  const [unit, setUnit] = useState<"usdt" | "coin">("usdt");
  const [amount, setAmount] = useState("");
  const [limitPx, setLimitPx] = useState("");
  const [tpPx, setTpPx] = useState("");
  const [slPx, setSlPx] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [tab, setTab] = useState<"open" | "history">("history");
  const pendingId = useRef<string | null>(null);

  const refresh = useCallback(() => {
    marketProvider
      .getPaperAccount(symbol)
      .then((a) => {
        setAcct(a);
        setLoadErr("");
      })
      .catch((e: unknown) => setLoadErr(errText(e, "common.loadFailed")));
  }, [symbol]);

  useEffect(() => {
    refresh();
    const timer = window.setInterval(() => !document.hidden && refresh(), POLL_MS);
    return () => window.clearInterval(timer);
  }, [refresh]);

  useEffect(() => {
    setLimitPx(price ? price.toFixed(pxDigits(price)) : "");
    setAmount("");
    setTpPx("");
    setSlPx("");
    setMsg(null);
  }, [symbol]); // eslint-disable-line react-hooks/exhaustive-deps

  const pos = acct?.positions.find((p) => p.symbol === symbol);
  const cost = acct?.costs[symbol] ?? { takerFee: 0.0005, makerFee: 0.0002, slippage: 0.0003 };
  const last = price ?? pos?.last ?? 0;
  const lp = num(limitPx);
  const refPx = otype === "limit" && lp > 0 ? lp : last;
  const amt = num(amount);
  const qty = amt > 0 && refPx > 0 ? (unit === "coin" ? amt : amt / refPx) : 0;
  const tp = num(tpPx);
  const sl = num(slPx);
  const tpsl = otype === "tpsl";
  const marketable = otype === "limit" && lp > 0 && last > 0 && (side === "buy" ? lp >= last : lp <= last);
  const asTaker = otype === "market" || marketable || tpsl;
  const feeRate = asTaker ? cost.takerFee : cost.makerFee;
  const slipRate = asTaker ? cost.slippage : 0;
  // TP/SL: estimate at the trigger (the stop leg when both are set)
  const execPx = tpsl ? (sl > 0 ? sl : tp) * (1 - slipRate) : asTaker ? (side === "buy" ? last * (1 + slipRate) : last * (1 - slipRate)) : refPx;
  const notional = qty * execPx;
  const fee = notional * feeRate;
  const slip = qty * last * slipRate;
  const total = side === "buy" ? notional + fee : notional - fee;
  const lim = acct?.limits;

  const check = useMemo(() => {
    if (!acct || !lim) return "";
    if (!(qty > 0)) return "";
    if (otype === "limit" && !(lp > 0)) return t("trade.chk.limit");
    if (tpsl) {
      if (!(tp > 0) && !(sl > 0)) return t("trade.chk.tpslEmpty");
      if (tp > 0 && !(tp > last)) return t("trade.chk.tpBelow");
      if (sl > 0 && !(sl < last)) return t("trade.chk.slAbove");
      for (const v of [tp, sl].filter((x) => x > 0)) {
        if (qty * v < lim.minOrderUsdt) return t("trade.chk.minTrig", { v: n(lim.minOrderUsdt, 0) });
        if (qty * v > lim.maxOrderUsdt) return t("trade.chk.maxTrig", { v: n(lim.maxOrderUsdt, 0) });
      }
      if (qty > (pos?.available ?? 0) + 1e-12) return t("trade.chk.posTpsl");
      return "";
    }
    if (notional < lim.minOrderUsdt) return t("trade.chk.min", { v: n(lim.minOrderUsdt, 0) });
    if (notional > lim.maxOrderUsdt) return t("trade.chk.max", { v: n(lim.maxOrderUsdt, 0) });
    if (side === "buy" && total > acct.availableCash) return t("trade.chk.cash");
    if (side === "buy" && (pos?.value ?? 0) + notional > lim.maxPositionUsdt) return t("trade.chk.posCap", { v: n(lim.maxPositionUsdt, 0) });
    if (side === "sell" && qty > (pos?.available ?? 0) + 1e-12) return t("trade.chk.noShort");
    return "";
  }, [acct, lim, qty, otype, lp, notional, side, total, pos, tpsl, tp, sl, last, t]);

  const setPct = (pct: number) => {
    if (!acct || !(refPx > 0)) return;
    if (side === "buy") {
      const room = Math.min(acct.availableCash / (1 + feeRate + slipRate), lim?.maxOrderUsdt ?? Infinity);
      const v = room * pct;
      setAmount(unit === "usdt" ? v.toFixed(2) : (v / refPx).toFixed(6));
    } else {
      const q = (pos?.available ?? 0) * pct;
      setAmount(unit === "coin" ? q.toFixed(6) : (q * refPx).toFixed(2));
    }
  };

  const submit = async () => {
    if (busy || !(qty > 0) || check) return;
    setBusy(true);
    setMsg(null);
    // one id per intended order: a retry after a network error reuses it, so it can't double-fill
    pendingId.current = pendingId.current ?? newId();
    try {
      const o = await marketProvider.placePaperOrder({
        client_order_id: pendingId.current,
        symbol,
        side,
        ...(unit === "coin" ? { qty: amt } : { notional: amt }),
        ...(tpsl
          ? tp > 0 && sl > 0
            ? { type: "oco" as const, take_profit_price: tp, stop_loss_price: sl }
            : { type: tp > 0 ? ("take_profit" as const) : ("stop_loss" as const), trigger_price: tp > 0 ? tp : sl }
          : { type: otype as "market" | "limit", ...(otype === "limit" ? { limit_price: lp } : {}) }),
      });
      if (tpsl) {
        setTpPx("");
        setSlPx("");
      }
      pendingId.current = null;
      setAmount("");
      setMsg({
        ok: true,
        text:
          tpsl
            ? t("trade.msg.tpslPlaced", {
                kind: (o.orders?.length ?? 1) > 1 ? t("trade.msg.oco") : o.type === "take_profit" ? t("trade.msg.tpOrder") : t("trade.msg.slOrder"),
                qty: n(o.qty, 6),
                sym: symbol,
              })
            : o.status === "filled"
            ? t(o.side === "buy" ? "trade.msg.filledBuy" : "trade.msg.filledSell", {
                qty: n(o.qty, 6),
                sym: symbol,
                px: n(o.fillPrice ?? 0, pxDigits(o.fillPrice ?? 1)),
                fee: n(o.fee, 4),
              })
            : t(o.side === "buy" ? "trade.msg.limitBuy" : "trade.msg.limitSell", { qty: n(o.qty, 6), px: n(o.limitPrice ?? 0, pxDigits(o.limitPrice ?? 1)) }),
      });
      refresh();
    } catch (e: unknown) {
      pendingId.current = null;
      setMsg({ ok: false, text: errText(e, "trade.orderFailed") });
    } finally {
      setBusy(false);
    }
  };

  const cancel = async (id: string) => {
    try {
      await marketProvider.cancelPaperOrder(id);
      refresh();
    } catch (e: unknown) {
      setMsg({ ok: false, text: errText(e, "trade.cancelFailed") });
    }
  };

  if (!acct) {
    return (
      <aside className="pt">
        <div className="pt-head"><b>{t("trade.title")}</b><span className="pt-lock">{t("trade.liveOff")}</span></div>
        <p className="muted pt-note">{loadErr || t("trade.loadingAcct")}</p>
      </aside>
    );
  }

  const d = pxDigits(last || 1);
  const openHere = acct.openOrders.filter((o) => o.symbol === symbol);
  return (
    <aside className="pt" aria-label={t("trade.title")}>
      <div className="pt-head">
        <b>{t("trade.title")}</b>
        <span className="pt-paper">PAPER</span>
        <span className="pt-lock" title={t("trade.liveLockTitle", { reason: acct.live.reason })}>🔒 {t("trade.liveOff")}</span>
      </div>

      <div className="pt-sides" role="tablist">
        <button type="button" className={`pt-buy${side === "buy" ? " is-on" : ""}`} onClick={() => { setSide("buy"); if (otype === "tpsl") setOtype("market"); }}>{t("trade.buy")}</button>
        <button type="button" className={`pt-sell${side === "sell" ? " is-on" : ""}`} onClick={() => setSide("sell")}>{t("trade.sell")}</button>
      </div>
      <div className="pt-row pt-types">
        {(["market", "limit", "tpsl"] as const).map((ot) => (
          <button
            key={ot}
            type="button"
            className={otype === ot ? "is-on" : ""}
            onClick={() => {
              setOtype(ot);
              if (ot === "tpsl") setSide("sell"); // protects an existing long
            }}
          >
            {ot === "market" ? t("trade.market") : ot === "limit" ? t("trade.limit") : t("trade.tpsl")}
          </button>
        ))}
        <span className="muted pt-last num">{t("trade.last")} {last ? n(last, d) : "—"}</span>
      </div>
      {otype === "limit" ? (
        <label className="pt-field">
          <span>{t("trade.limitPx")}</span>
          <input inputMode="decimal" value={limitPx} onChange={(e) => setLimitPx(e.target.value)} />
        </label>
      ) : null}
      {tpsl ? (
        <>
          <label className="pt-field">
            <span>{t("trade.tpPx")}</span>
            <input inputMode="decimal" placeholder={t("trade.tpPh")} value={tpPx} onChange={(e) => setTpPx(e.target.value)} />
          </label>
          <label className="pt-field">
            <span>{t("trade.slPx")}</span>
            <input inputMode="decimal" placeholder={t("trade.slPh")} value={slPx} onChange={(e) => setSlPx(e.target.value)} />
          </label>
        </>
      ) : null}
      <label className="pt-field">
        <span>
          {unit === "usdt" ? t("trade.amountUsdt") : t("trade.qtyCoin", { sym: symbol })}
          <button type="button" className="pt-unit" onClick={() => { setUnit(unit === "usdt" ? "coin" : "usdt"); setAmount(""); }}>
            {unit === "usdt" ? t("trade.byQty") : t("trade.byAmount")}
          </button>
        </span>
        <input inputMode="decimal" placeholder="0" value={amount} onChange={(e) => setAmount(e.target.value)} />
      </label>
      <div className="pt-row pt-pcts">
        {[0.25, 0.5, 0.75, 1].map((p) => (
          <button key={p} type="button" onClick={() => setPct(p)}>{p * 100}%</button>
        ))}
      </div>

      <dl className="pt-est num">
        <div><dt>{t("ob.qty")}</dt><dd>{qty > 0 ? n(qty, 6) : "—"} {symbol}</dd></div>
        {tpsl ? (
          <>
            <div><dt>{t("trade.tpProceeds")}</dt><dd>{qty > 0 && tp > 0 ? `${n(qty * tp * (1 - slipRate) * (1 - feeRate), 2)} USDT` : "—"}</dd></div>
            <div><dt>{t("trade.slProceeds")}</dt><dd>{qty > 0 && sl > 0 ? `${n(qty * sl * (1 - slipRate) * (1 - feeRate), 2)} USDT` : "—"}</dd></div>
          </>
        ) : (
          <div><dt>{t("trade.estPx")}</dt><dd>{qty > 0 ? n(execPx, d) : "—"}</dd></div>
        )}
        <div><dt>{t("trade.fee")} {fmtFixed(feeRate * 100, 3)}% <small className="muted">{asTaker ? t("trade.taker") : t("trade.maker")}</small></dt><dd>{qty > 0 ? n(fee, 4) : "—"}</dd></div>
        <div><dt>{t("trade.slippage")} {fmtFixed(slipRate * 1e4, 0)}bp</dt><dd>{qty > 0 ? n(slip, 4) : "—"}</dd></div>
        {tpsl ? null : <div className="pt-total"><dt>{side === "buy" ? t("trade.totalCost") : t("trade.estProceeds")}</dt><dd>{qty > 0 ? `${n(total, 2)} USDT` : "—"}</dd></div>}
      </dl>
      {check ? <p className="pt-warn">{check}</p> : null}
      <button type="button" className={`pt-submit ${side}`} disabled={busy || !(qty > 0) || !!check} onClick={submit}>
        {busy
          ? t("common.submitting")
          : tpsl
          ? t(tp > 0 && sl > 0 ? "trade.submitOco" : "trade.submitTpsl", { sym: symbol })
          : t(side === "buy" ? "trade.submitBuy" : "trade.submitSell", { sym: symbol })}
      </button>
      {msg ? <p className={msg.ok ? "pt-ok" : "pt-warn"}>{msg.text}</p> : null}
      {tpsl ? (
        <p className="muted pt-note">
          {t("trade.tpslNote")}
        </p>
      ) : null}
      <p className="muted pt-note">
        {t("trade.limitsNote", { cash: n(acct.availableCash), order: n(acct.limits.maxOrderUsdt, 0), pos: n(acct.limits.maxPositionUsdt, 0) })}
      </p>

      <div className="pt-box">
        <div className="pt-box-head"><b>{t("trade.position", { sym: symbol })}</b></div>
        {pos && pos.qty > 0 ? (
          <dl className="pt-est num">
            <div><dt>{t("ob.qty")}</dt><dd>{n(pos.qty, 6)}</dd></div>
            <div><dt>{t("trade.avgLast")}</dt><dd>{n(pos.avgPrice, d)} / {n(pos.last, d)}</dd></div>
            <div><dt>{t("trade.value")}</dt><dd>{n(pos.value)} USDT</dd></div>
            <div><dt>{t("trade.upnl")}</dt><dd className={tone(pos.unrealized)}>{signed(pos.unrealized)} ({signed((pos.last / pos.avgPrice - 1) * 100)}%)</dd></div>
            <div><dt>{t("trade.realized")}</dt><dd className={tone(pos.realized)}>{signed(pos.realized)}</dd></div>
          </dl>
        ) : (
          <p className="muted pt-note">{t("trade.noPos")}{pos && pos.realized ? ` · ${t("trade.realizedShort", { v: signed(pos.realized) })}` : ""}</p>
        )}
        <dl className="pt-est num pt-acct">
          <div><dt>{t("trade.equity")}</dt><dd>{n(acct.equity)} USDT</dd></div>
          <div><dt>{t("trade.totalPnl")}</dt><dd className={tone(acct.pnl)}>{signed(acct.pnl)} ({signed(acct.pnlPct * 100)}%)</dd></div>
        </dl>
      </div>

      <div className="pt-box">
        <div className="pt-box-head pt-tabs">
          <button type="button" className={tab === "open" ? "is-on" : ""} onClick={() => setTab("open")}>{t("trade.openOrders")} {openHere.length}</button>
          <button type="button" className={tab === "history" ? "is-on" : ""} onClick={() => setTab("history")}>{t("trade.history")}</button>
        </div>
        <ul className="pt-orders num">
          {(tab === "open" ? openHere : acct.orders).slice(0, 20).map((o) => (
            <li key={o.id}>
              <span className={o.side === "buy" ? "up" : "down"}>{o.side === "buy" ? t("chart.buy") : t("chart.sell")}·{typeLabel(o.type)}{o.ocoGroup ? "·OCO" : ""}</span>
              <span>
                {n(o.qty, 6)} @ {o.status !== "filled" && o.triggerPrice ? t("trade.trigger", { px: n(o.triggerPrice, pxDigits(o.triggerPrice)) }) : n(o.fillPrice ?? o.limitPrice ?? 0, pxDigits(o.fillPrice ?? o.limitPrice ?? 1))}
              </span>
              <span className="muted">
                {t(`trade.status.${o.status}`)}{o.status === "filled" ? ` · ${t("trade.feeShort", { v: n(o.fee, 3) })}` : ""}{o.reason && REASONS.has(o.reason) && o.status !== "open" ? ` · ${t(`trade.reason.${o.reason}`)}` : ""}
              </span>
              <span className="muted">{hhmm(o.fillTs ?? o.ts)}</span>
              {o.status === "open" ? <button type="button" onClick={() => cancel(o.id)}>{t("trade.cancel")}</button> : null}
            </li>
          ))}
          {(tab === "open" ? openHere : acct.orders).length === 0 ? <li className="muted">{t("common.none")}</li> : null}
        </ul>
      </div>
    </aside>
  );
}
