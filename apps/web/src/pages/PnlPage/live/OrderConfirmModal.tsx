import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { ApiError, marketProvider, type ExchangePosition, type ExchangeQuote } from "@/providers/HttpWsProvider";
import { errText } from "@/i18n/errors";
import { newClientId } from "@/lib/clientId";
import type { Market } from "@/pages/PnlPage/live/useLive";

export type Intent = {
  ex: string;
  market: Market;
  symbol: string; // full symbol: BTC/USDT or BTC/USDT:USDT
  side: "buy" | "sell";
  type: "market" | "limit";
  amount: number;
  price?: number; // limit orders
  /** futures close: reduce-only market close of the user's own position (side / size come from the exchange) */
  close?: boolean;
  position?: ExchangePosition;
  leverage?: number;
  marginMode?: "cross" | "isolated";
};

export type Receipt = { kind: "ok" | "err"; text: string };

const fmt = (v: number | null | undefined, d = 6) => (v == null || Number.isNaN(Number(v)) ? "—" : Number(v).toLocaleString(undefined, { maximumFractionDigits: d }));

/**
 * The second confirmation for EVERY live order. Shows exchange, instrument, side, amount, estimated fill price and
 * leverage / margin mode. One clientOrderId per opening of the dialog: a double click or a retry after an unknown result
 * re-sends the same id, which the server de-duplicates (and looks up instead of re-sending).
 */
export function OrderConfirmModal({ intent, onCancel, onDone }: { intent: Intent; onCancel: () => void; onDone: (r: Receipt) => void }) {
  const { t } = useTranslation();
  const [coid, setCoid] = useState(() => newClientId("", 20));
  const [quote, setQuote] = useState<ExchangeQuote | null>(null);
  const [quoteErr, setQuoteErr] = useState<string>("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const lock = useRef(false);

  useEffect(() => {
    let alive = true;
    marketProvider.exchangeQuote(intent.ex, intent.symbol, intent.market)
      .then((q) => alive && setQuote(q))
      .catch((e: unknown) => alive && setQuoteErr(errText(e, "live.confirm.quoteFailed")));
    return () => { alive = false; };
  }, [intent.ex, intent.symbol, intent.market]);

  const closing = !!intent.close;
  const side = closing ? (intent.position?.side === "short" ? "buy" : "sell") : intent.side;
  const amount = closing ? Math.abs(Number(intent.position?.contracts ?? 0)) : intent.amount;
  const px = intent.type === "limit" && !closing
    ? intent.price ?? null
    : side === "buy" ? quote?.ask ?? quote?.last ?? null : quote?.bid ?? quote?.last ?? null;
  const size = intent.market === "swap" ? quote?.contractSize ?? 1 : 1;
  const notional = px != null ? amount * size * px : null;
  const ready = !!quote && px != null && px > 0 && amount > 0;
  const lev = closing ? intent.position?.leverage ?? null : intent.leverage ?? null;
  const mm = closing ? intent.position?.marginMode ?? null : intent.marginMode ?? null;
  const sideLabel = intent.market === "swap" && !closing
    ? side === "buy" ? t("live.side.long") : t("live.side.short")
    : side === "buy" ? t("live.side.buy") : t("live.side.sell");

  const submit = async () => {
    if (lock.current || !ready) return;
    lock.current = true;
    setBusy(true);
    setErr("");
    try {
      let receipt: Receipt;
      if (closing) {
        const r = await marketProvider.exchangeFuturesClose(intent.ex, intent.symbol, coid);
        receipt = r.partialError
          ? { kind: "err", text: t("live.result.closePartial", { msg: r.partialError }) }
          : { kind: "ok", text: r.duplicate ? t("live.result.dup") : t("live.result.close", { symbol: intent.symbol }) };
      } else if (intent.market === "swap") {
        const r = await marketProvider.exchangeFuturesPlace(intent.ex, {
          clientOrderId: coid, symbol: intent.symbol, side, type: intent.type, amount, price: intent.type === "limit" ? intent.price ?? null : null,
          reduceOnly: false, confirmed: true, leverage: intent.leverage, marginMode: intent.marginMode,
        });
        receipt = { kind: "ok", text: r.duplicate ? t("live.result.dup") : t("live.result.ok", { symbol: r.order.symbol, side: sideLabel, amount: fmt(amount), status: r.order.status }) };
      } else {
        const r = await marketProvider.exchangePlace(intent.ex, {
          clientOrderId: coid, symbol: intent.symbol, side, type: intent.type, amount, price: intent.type === "limit" ? intent.price ?? null : null, confirmed: true,
        });
        receipt = { kind: "ok", text: r.duplicate ? t("live.result.dup") : t("live.result.ok", { symbol: r.order.symbol, side: sideLabel, amount: fmt(amount), status: r.order.status }) };
      }
      onDone(receipt);
    } catch (e) {
      setErr(errText(e, "common.requestFailed"));
      // Unknown result: keep the id so a retry looks the order up. Any other answer (the server recorded it as rejected): new id.
      if (!(e instanceof ApiError && e.code === "ORDER_UNKNOWN")) setCoid(newClientId("", 20));
    } finally {
      lock.current = false;
      setBusy(false);
    }
  };

  const row = (k: string, v: React.ReactNode) => (
    <div className="live-confirm-row"><span>{k}</span><b>{v}</b></div>
  );

  return (
    <div className="modal-backdrop" role="presentation">
      <div className="modal-card live-modal" role="dialog" aria-modal="true" aria-label={closing ? t("live.confirm.titleClose") : t("live.confirm.title")} data-testid="live-confirm">
        <h2>{closing ? t("live.confirm.titleClose") : t("live.confirm.title")}</h2>
        <div className="live-confirm-grid">
          {row(t("live.confirm.exchange"), t(`live.ex.${intent.ex}`))}
          {row(t("live.confirm.symbol"), intent.symbol)}
          {row(t("live.confirm.market"), intent.market === "swap" ? t("live.confirm.swap") : t("live.confirm.spot"))}
          {row(t("live.confirm.side"), <span className={side === "buy" ? "up" : "down"}>{sideLabel}</span>)}
          {row(t("live.confirm.amount"), fmt(amount))}
          {row(t("live.confirm.type"), closing || intent.type === "market" ? t("live.confirm.marketType") : t("live.confirm.limitType", { p: fmt(intent.price) }))}
          {row(t("live.confirm.est"), quote ? fmt(px, 6) : quoteErr ? <span className="down">—</span> : t("live.confirm.quoteLoading"))}
          {row(t("live.confirm.notional"), notional != null ? fmt(notional, 2) : "—")}
          {intent.market === "swap"
            ? row(t("live.confirm.leverage"), `${lev ?? "—"}x · ${mm === "cross" ? t("live.cross") : mm === "isolated" ? t("live.isolated") : "—"}`)
            : row(t("live.confirm.leverage"), t("live.confirm.spot"))}
        </div>
        {closing ? <div className="pro-alert is-ok live-reduce">{t("live.confirm.reduceOnly")}</div> : null}
        {quoteErr ? <div className="pro-alert" role="alert">{quoteErr}</div> : null}
        <div className="pro-alert exch-risk">{t("live.confirm.risk")}</div>
        <p className="td-note live-coid">{t("live.confirm.id", { id: coid.slice(0, 8) })}</p>
        {err ? <div className="pro-alert" role="alert" data-testid="live-confirm-err">{err}</div> : null}
        <div className="close-modal-actions">
          <button type="button" className="td-link" disabled={busy} onClick={onCancel}>{t("live.confirm.cancel")}</button>
          <button type="button" className="td-submit close-go" data-testid="live-confirm-go" disabled={busy || !ready} onClick={submit}>
            {busy ? t("live.confirm.busy") : closing ? t("live.confirm.goClose") : t("live.confirm.go")}
          </button>
        </div>
      </div>
    </div>
  );
}
