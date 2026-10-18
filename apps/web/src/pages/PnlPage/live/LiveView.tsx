import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { marketProvider, type ExchangeConfig, type TradeGate } from "@/providers/HttpWsProvider";
import { errText } from "@/i18n/errors";
import { fmtNum } from "@/i18n/format";
import { Empty, Sk } from "@/components/ui/Skeleton";
import { tone } from "@/pages/PnlPage/usePnl";
import { OrderConfirmModal, type Intent, type Receipt } from "@/pages/PnlPage/live/OrderConfirmModal";
import { STABLES, canRead, normSymbol, useAvailability, useLiveRows, type Market } from "@/pages/PnlPage/live/useLive";

const EXCHANGES = ["binance", "okx"] as const;
const num = (v: number | null | undefined, d = 6) => (v == null || Number.isNaN(Number(v)) ? "—" : fmtNum(Number(v), { maximumFractionDigits: d }));

function reasonText(t: (k: string) => string, reason: string | null | undefined) {
  const key = `live.reason.${reason ?? "UNKNOWN"}`;
  const s = t(key);
  return s === key ? t("live.reason.UNKNOWN") : s;
}

/** "Available" / "Unavailable" badge plus the reason; every disabled control below carries the same reason as its title. */
function GateBadge({ gate, label }: { gate: TradeGate | undefined; label?: string }) {
  const { t } = useTranslation();
  if (!gate) return null;
  return (
    <div className={`live-gate ${gate.available ? "is-ok" : "is-no"}`} data-testid="live-gate" data-available={gate.available ? "1" : "0"}>
      <b>{label ? `${label} · ` : ""}{gate.available ? t("live.state.ok") : t("live.state.no")}</b>
      {gate.available ? null : (
        <>
          <span data-testid="live-gate-reason" data-reason={gate.reason ?? ""}>{reasonText(t, gate.reason)}</span>
          {gate.reason && ["NOT_BOUND", "KEY_NO_TRADE", "KEY_NO_FUTURES", "FUTURES_ACK_REQUIRED", "KEY_HAS_WITHDRAW"].includes(gate.reason) ? (
            <Link className="td-link" to="/exchange">{t("live.fix")} ›</Link>
          ) : null}
        </>
      )}
    </div>
  );
}

function useSymbolCheck(ex: string, symbol: string, market: Market, on: boolean) {
  const [state, setState] = useState<"idle" | "checking" | "ok" | "no">("idle");
  useEffect(() => {
    if (!on || !symbol) { setState("idle"); return; }
    let alive = true;
    setState("checking");
    const id = window.setTimeout(() => {
      marketProvider.exchangeSupported(ex, [symbol], market)
        .then((r) => alive && setState(Object.values(r.supported)[0] ? "ok" : "no"))
        .catch(() => alive && setState("idle"));
    }, 350);
    return () => { alive = false; window.clearTimeout(id); };
  }, [ex, symbol, market, on]);
  return state;
}

function Ticket({ ex, market, gate, cfg, onIntent }: { ex: string; market: Market; gate: TradeGate | undefined; cfg: ExchangeConfig | null; onIntent: (i: Intent) => void }) {
  const { t } = useTranslation();
  const [sym, setSym] = useState("BTC");
  const [side, setSide] = useState<"buy" | "sell">("buy");
  const [type, setType] = useState<"market" | "limit">("market");
  const [price, setPrice] = useState("");
  const [amount, setAmount] = useState("");
  const [lev, setLev] = useState(2);
  const [mode, setMode] = useState<"cross" | "isolated">("isolated");
  const open = !!gate?.available;
  const full = normSymbol(sym, market);
  const check = useSymbolCheck(ex, full, market, open);
  const amountN = Number(amount);
  const priceN = Number(price);
  const valid = amountN > 0 && (type === "market" || priceN > 0) && check === "ok";
  const maxLev = cfg?.maxLeverage ?? 5;
  const reason = !open ? reasonText(t, gate?.reason) : check === "no" ? reasonText(t, "SYMBOL_UNSUPPORTED") : undefined;
  return (
    <fieldset className="live-ticket" disabled={!open} data-testid={`ticket-${market}`}>
      <legend>{market === "spot" ? t("live.ticket.spot") : t("live.ticket.swap")}</legend>
      <div className="exch-form">
        <label className="pt-field"><span>{t("live.ticket.symbol")}</span>
          <input value={sym} onChange={(e) => setSym(e.target.value.toUpperCase())} maxLength={24} spellCheck={false} data-testid={`ticket-symbol-${market}`} />
        </label>
        <label className="pt-field"><span>{t("live.ticket.side")}</span>
          <select value={side} onChange={(e) => setSide(e.target.value as "buy" | "sell")}>
            <option value="buy">{market === "swap" ? t("live.side.long") : t("live.side.buy")}</option>
            <option value="sell">{market === "swap" ? t("live.side.short") : t("live.side.sell")}</option>
          </select>
        </label>
        <label className="pt-field"><span>{t("live.ticket.type")}</span>
          <select value={type} onChange={(e) => setType(e.target.value as "market" | "limit")}>
            <option value="market">{t("live.type.market")}</option><option value="limit">{t("live.type.limit")}</option>
          </select>
        </label>
        {type === "limit" ? (
          <label className="pt-field"><span>{t("live.ticket.price")}</span>
            <input inputMode="decimal" value={price} onChange={(e) => setPrice(e.target.value)} maxLength={20} />
          </label>
        ) : null}
        <label className="pt-field"><span>{t("live.ticket.amount")}</span>
          <input inputMode="decimal" value={amount} onChange={(e) => setAmount(e.target.value)} maxLength={20} data-testid={`ticket-amount-${market}`} />
        </label>
        {market === "swap" ? (
          <>
            <label className="pt-field"><span>{t("live.ticket.leverage")}</span>
              <select value={lev} onChange={(e) => setLev(Number(e.target.value))}>
                {Array.from({ length: maxLev }, (_, i) => i + 1).map((n) => <option key={n} value={n}>{n}x</option>)}
              </select>
            </label>
            <label className="pt-field"><span>{t("live.ticket.margin")}</span>
              <select value={mode} onChange={(e) => setMode(e.target.value as "cross" | "isolated")}>
                <option value="isolated">{t("live.isolated")}</option><option value="cross">{t("live.cross")}</option>
              </select>
            </label>
          </>
        ) : null}
        <div className="exch-full">
          <button
            type="button"
            className="td-submit"
            data-testid={`ticket-go-${market}`}
            disabled={!open || !valid}
            title={reason}
            onClick={() => onIntent({ ex, market, symbol: full, side, type, amount: amountN, price: type === "limit" ? priceN : undefined, leverage: market === "swap" ? lev : undefined, marginMode: market === "swap" ? mode : undefined })}
          >
            {t("live.ticket.go")}
          </button>
          {check === "checking" ? <span className="muted"> {t("live.ticket.checking")}</span> : null}
          {open && check === "no" ? <span className="down live-inline-reason" data-testid="ticket-unsupported"> {reasonText(t, "SYMBOL_UNSUPPORTED")}</span> : null}
        </div>
      </div>
      <p className="td-note">
        {market === "spot" ? t("live.ticket.capSpot", { cap: num(cfg?.maxOrderUsdt, 2) }) : t("live.ticket.capSwap", { cap: num(cfg?.maxFuturesUsdt, 2), lev: maxLev })}
        {market === "swap" && ex === "okx" ? ` ${t("live.ticket.okxUnit")}` : ""}
      </p>
    </fieldset>
  );
}

function Section({ ex, market, gates, cfg, onIntent, bumpKey }: {
  ex: string; market: Market; gates: { open: TradeGate; close: TradeGate } | undefined; cfg: ExchangeConfig | null; onIntent: (i: Intent) => void; bumpKey: number;
}) {
  const { t } = useTranslation();
  const readable = !!gates && canRead(gates.open.reason) || (!!gates && market === "swap" && canRead(gates.close.reason));
  const rows = useLiveRows(ex, market, readable);
  useEffect(() => { if (bumpKey) rows.reload(); }, [bumpKey]); // eslint-disable-line react-hooks/exhaustive-deps
  const closeGate = gates?.close;
  const sellable = useMemo(() => (rows.balances ?? []).filter((b) => !STABLES.has(b.asset)), [rows.balances]);
  return (
    <section className="pnl-section pane live-section" data-testid={`live-${market}`}>
      <div className="pnl-sec-head">
        <h2>{market === "spot" ? t("live.section.spot") : t("live.section.swap")}</h2>
        <GateBadge gate={gates?.open} />
      </div>
      {market === "swap" && gates && !gates.open.available && gates.close.available ? <GateBadge gate={gates.close} label={t("live.close")} /> : null}

      {!readable ? null : rows.error ? (
        <div className="pro-alert">{errText(rows.error, "common.loadFailed")}</div>
      ) : market === "spot" ? (
        rows.balances == null ? <Sk w={320} h={40} /> : rows.balances.length === 0 ? <p className="muted">{t("live.noBalances")}</p> : (
          <div className="pro-table-wrap">
            <table className="pro-table pnl-table live-table">
              <thead><tr><th>{t("live.col.asset")}</th><th>{t("live.col.total")}</th><th>{t("live.col.free")}</th><th>{t("live.col.used")}</th><th>{t("live.col.action")}</th></tr></thead>
              <tbody>
                {rows.balances.map((b) => {
                  const stable = STABLES.has(b.asset);
                  const supported = rows.supported[b.asset] !== false;
                  const gate = gates?.close;
                  const can = !!gate?.available && supported && b.free > 0;
                  const why = !gate?.available ? reasonText(t, gate?.reason) : !supported ? reasonText(t, "SYMBOL_UNSUPPORTED") : undefined;
                  return (
                    <tr key={b.asset}>
                      <td><b>{b.asset}</b></td><td className="num">{num(b.total)}</td><td className="num">{num(b.free)}</td><td className="num">{num(b.used)}</td>
                      <td>
                        {stable ? "—" : (
                          <button type="button" className="pnl-close-btn" data-testid={`sell-${b.asset}`} disabled={!can} title={why} aria-disabled={!can}
                            onClick={() => onIntent({ ex, market: "spot", symbol: `${b.asset}/USDT`, side: "sell", type: "market", amount: b.free })}>
                            {t("live.sellAll")}
                          </button>
                        )}
                        {!stable && !supported ? <span className="pnl-stale-tag" data-testid={`unsupported-${b.asset}`}>{t("live.state.no")}</span> : null}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
            {sellable.length === 0 ? null : null}
          </div>
        )
      ) : (
        rows.positions == null ? <Sk w={320} h={40} /> : rows.positions.length === 0 ? <Empty title={t("live.noPositions")} /> : (
          <div className="pro-table-wrap">
            <table className="pro-table pnl-table live-table">
              <thead>
                <tr>
                  <th>{t("live.col.symbol")}</th><th>{t("live.col.side")}</th><th>{t("live.col.size")}</th><th>{t("live.col.entry")}</th><th>{t("live.col.mark")}</th>
                  <th>{t("live.col.liq")}</th><th>{t("live.col.upnl")}</th><th>{t("live.col.lev")}</th><th>{t("live.col.margin")}</th><th>{t("live.col.action")}</th>
                </tr>
              </thead>
              <tbody>
                {rows.positions.map((p) => (
                  <tr key={`${p.symbol}-${p.side}`}>
                    <td><b>{p.symbol}</b></td>
                    <td className={p.side === "short" ? "down" : "up"}>{p.side === "short" ? t("live.short") : t("live.long")}</td>
                    <td className="num">{num(Math.abs(Number(p.contracts)))}</td><td className="num">{num(p.entryPrice, 4)}</td><td className="num">{num(p.markPrice, 4)}</td>
                    <td className="num">{num(p.liquidationPrice, 4)}</td>
                    <td className={`num ${tone(p.unrealizedPnl ?? 0)}`}>{num(p.unrealizedPnl, 4)}</td>
                    <td className="num">{p.leverage ? `${p.leverage}x` : "—"}</td>
                    <td>{p.marginMode === "cross" ? t("live.cross") : p.marginMode === "isolated" ? t("live.isolated") : "—"}</td>
                    <td>
                      <button type="button" className="pnl-close-btn" data-testid={`close-live-${p.symbol}`} disabled={!closeGate?.available} aria-disabled={!closeGate?.available}
                        title={closeGate?.available ? t("live.closeHint") : reasonText(t, closeGate?.reason)}
                        onClick={() => onIntent({ ex, market: "swap", symbol: p.symbol, side: p.side === "short" ? "buy" : "sell", type: "market", amount: Math.abs(Number(p.contracts)), close: true, position: p })}>
                        {t("live.close")} <span className="live-ro">{t("live.reduceOnlyTag")}</span>
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )
      )}
      <Ticket ex={ex} market={market} gate={gates?.open} cfg={cfg} onIntent={onIntent} />
    </section>
  );
}

/** 实盘视图: balances / positions of the user's own linked exchange accounts, with confirmed, idempotent orders. */
export function LiveView() {
  const { t } = useTranslation();
  const [ex, setEx] = useState<(typeof EXCHANGES)[number]>("binance");
  const av = useAvailability();
  const [intent, setIntent] = useState<Intent | null>(null);
  const [note, setNote] = useState<Receipt | null>(null);
  const [bump, setBump] = useState(0);
  const cur = av.data?.exchanges[ex];
  return (
    <>
      <div className="pro-alert is-warn live-real" role="note">{t("live.real")}</div>
      <p className="muted live-sub">{t("live.sub")}</p>
      <div className="exch-tabs live-ex-tabs" role="tablist">
        {EXCHANGES.map((x) => (
          <button key={x} type="button" role="tab" aria-selected={ex === x} className={ex === x ? "is-on" : ""} data-testid={`live-ex-${x}`} onClick={() => setEx(x)}>
            {t(`live.ex.${x}`)}{av.data?.exchanges[x]?.bound ? " ●" : ""}
          </button>
        ))}
        <button type="button" className="td-link" onClick={() => { av.reload(); setBump((n) => n + 1); }}>{t("live.refresh")}</button>
        <Link className="td-link live-manage" to="/exchange">{t("live.manage")} ›</Link>
      </div>
      {av.error && !av.data ? <div className="pro-alert">{errText(av.error, "common.loadFailed")}</div> : null}
      {note ? <div className={`pro-alert ${note.kind === "ok" ? "is-ok" : ""}`} role="status" aria-live="polite" data-testid="live-note">{note.text}</div> : null}
      {!av.data ? <Sk w={320} h={80} /> : (
        <>
          <Section ex={ex} market="spot" gates={cur?.spot} cfg={av.cfg} onIntent={setIntent} bumpKey={bump} />
          <Section ex={ex} market="swap" gates={cur?.swap} cfg={av.cfg} onIntent={setIntent} bumpKey={bump} />
        </>
      )}
      {intent ? (
        <OrderConfirmModal
          intent={intent}
          onCancel={() => setIntent(null)}
          onDone={(r) => { setIntent(null); setNote(r); av.reload(); setBump((n) => n + 1); }}
        />
      ) : null}
    </>
  );
}
