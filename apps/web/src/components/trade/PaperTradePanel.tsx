import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { marketProvider } from "@/providers/HttpWsProvider";
import type { PaperAccount, PaperOrder } from "@/types/mainstream";

/** Paper-only order ticket + position + orders for one coin. Live trading stays locked server-side. */

type Side = "buy" | "sell";
type OType = "market" | "limit" | "tpsl";

const POLL_MS = 5_000;

function n(v: number, d = 2) {
  return Number.isFinite(v) ? v.toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d }) : "—";
}
function pxDigits(px: number) {
  return px >= 1000 ? 2 : px >= 10 ? 2 : px >= 1 ? 3 : 5;
}
function signed(v: number, d = 2) {
  return `${v > 0 ? "+" : ""}${n(v, d)}`;
}
function tone(v: number) {
  return v > 0 ? "up" : v < 0 ? "down" : "";
}
function hhmm(ms: number) {
  const t = new Date(ms);
  const p = (x: number) => String(x).padStart(2, "0");
  return `${p(t.getMonth() + 1)}-${p(t.getDate())} ${p(t.getHours())}:${p(t.getMinutes())}:${p(t.getSeconds())}`;
}
function newId() {
  const c = globalThis.crypto as Crypto | undefined;
  return c?.randomUUID ? c.randomUUID() : `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 12)}`;
}

const STATUS: Record<PaperOrder["status"], string> = { open: "挂单中", filled: "已成交", cancelled: "已撤销", rejected: "已拒绝" };
const TYPE: Record<PaperOrder["type"], string> = { market: "市", limit: "限", take_profit: "止盈", stop_loss: "止损" };
const REASON: Record<string, string> = { OCO_CANCEL: "另一腿已成交/撤销", TP_TRIGGERED: "止盈触发", SL_TRIGGERED: "止损触发", INSUFFICIENT_POSITION: "持仓不足" };

export function PaperTradePanel({ symbol, price }: { symbol: string; price: number | null | undefined }) {
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
      .catch((e: unknown) => setLoadErr(e instanceof Error ? e.message : "读取失败"));
  }, [symbol]);

  useEffect(() => {
    refresh();
    const t = window.setInterval(() => !document.hidden && refresh(), POLL_MS);
    return () => window.clearInterval(t);
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
  const lp = Number(limitPx);
  const refPx = otype === "limit" && lp > 0 ? lp : last;
  const amt = Number(amount);
  const qty = amt > 0 && refPx > 0 ? (unit === "coin" ? amt : amt / refPx) : 0;
  const tp = Number(tpPx);
  const sl = Number(slPx);
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
    if (otype === "limit" && !(lp > 0)) return "请填写限价";
    if (tpsl) {
      if (!(tp > 0) && !(sl > 0)) return "请填写止盈价或止损价（都填 = OCO，一边成交另一边自动撤销）";
      if (tp > 0 && !(tp > last)) return "止盈价必须高于现价";
      if (sl > 0 && !(sl < last)) return "止损价必须低于现价";
      for (const v of [tp, sl].filter((x) => x > 0)) {
        if (qty * v < lim.minOrderUsdt) return `单笔至少 ${lim.minOrderUsdt} USDT（按触发价）`;
        if (qty * v > lim.maxOrderUsdt) return `超过单笔上限 ${n(lim.maxOrderUsdt, 0)} USDT（按触发价）`;
      }
      if (qty > (pos?.available ?? 0) + 1e-12) return "可卖数量不足（止盈止损只保护已有持仓）";
      return "";
    }
    if (notional < lim.minOrderUsdt) return `单笔至少 ${lim.minOrderUsdt} USDT`;
    if (notional > lim.maxOrderUsdt) return `超过单笔上限 ${n(lim.maxOrderUsdt, 0)} USDT`;
    if (side === "buy" && total > acct.availableCash) return "可用资金不足";
    if (side === "buy" && (pos?.value ?? 0) + notional > lim.maxPositionUsdt) return `超过单币持仓上限 ${n(lim.maxPositionUsdt, 0)} USDT`;
    if (side === "sell" && qty > (pos?.available ?? 0) + 1e-12) return "可卖数量不足（不能做空）";
    return "";
  }, [acct, lim, qty, otype, lp, notional, side, total, pos, tpsl, tp, sl, last]);

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
            ? `${(o.orders?.length ?? 1) > 1 ? "OCO 止盈止损" : o.type === "take_profit" ? "止盈单" : "止损单"}已挂出：卖 ${n(o.qty, 6)} ${symbol}，触发后按市价成交`
            : o.status === "filled"
            ? `纸面${o.side === "buy" ? "买入" : "卖出"} ${n(o.qty, 6)} ${symbol} @ ${n(o.fillPrice ?? 0, pxDigits(o.fillPrice ?? 1))}，手续费 ${n(o.fee, 4)} USDT`
            : `限价单已挂出：${o.side === "buy" ? "买" : "卖"} ${n(o.qty, 6)} @ ${n(o.limitPrice ?? 0, pxDigits(o.limitPrice ?? 1))}`,
      });
      refresh();
    } catch (e: unknown) {
      pendingId.current = null;
      setMsg({ ok: false, text: e instanceof Error ? e.message : "下单失败" });
    } finally {
      setBusy(false);
    }
  };

  const cancel = async (id: string) => {
    try {
      await marketProvider.cancelPaperOrder(id);
      refresh();
    } catch (e: unknown) {
      setMsg({ ok: false, text: e instanceof Error ? e.message : "撤单失败" });
    }
  };

  if (!acct) {
    return (
      <aside className="pt">
        <div className="pt-head"><b>纸面交易</b><span className="pt-lock">实盘未开启</span></div>
        <p className="muted pt-note">{loadErr || "加载账户…"}</p>
      </aside>
    );
  }

  const d = pxDigits(last || 1);
  const openHere = acct.openOrders.filter((o) => o.symbol === symbol);
  return (
    <aside className="pt" aria-label="纸面交易">
      <div className="pt-head">
        <b>纸面交易</b>
        <span className="pt-paper">PAPER</span>
        <span className="pt-lock" title={`${acct.live.reason}：网页端不能开启实盘`}>🔒 {acct.live.message}</span>
      </div>

      <div className="pt-sides" role="tablist">
        <button type="button" className={`pt-buy${side === "buy" ? " is-on" : ""}`} onClick={() => { setSide("buy"); if (otype === "tpsl") setOtype("market"); }}>买入</button>
        <button type="button" className={`pt-sell${side === "sell" ? " is-on" : ""}`} onClick={() => setSide("sell")}>卖出</button>
      </div>
      <div className="pt-row pt-types">
        {(["market", "limit", "tpsl"] as const).map((t) => (
          <button
            key={t}
            type="button"
            className={otype === t ? "is-on" : ""}
            onClick={() => {
              setOtype(t);
              if (t === "tpsl") setSide("sell"); // protects an existing long
            }}
          >
            {t === "market" ? "市价" : t === "limit" ? "限价" : "止盈止损"}
          </button>
        ))}
        <span className="muted pt-last num">现价 {last ? n(last, d) : "—"}</span>
      </div>
      {otype === "limit" ? (
        <label className="pt-field">
          <span>限价 (USDT)</span>
          <input inputMode="decimal" value={limitPx} onChange={(e) => setLimitPx(e.target.value)} />
        </label>
      ) : null}
      {tpsl ? (
        <>
          <label className="pt-field">
            <span>止盈触发价 (USDT，高于现价)</span>
            <input inputMode="decimal" placeholder="不填则只设止损" value={tpPx} onChange={(e) => setTpPx(e.target.value)} />
          </label>
          <label className="pt-field">
            <span>止损触发价 (USDT，低于现价)</span>
            <input inputMode="decimal" placeholder="不填则只设止盈" value={slPx} onChange={(e) => setSlPx(e.target.value)} />
          </label>
        </>
      ) : null}
      <label className="pt-field">
        <span>
          {unit === "usdt" ? "金额 (USDT)" : `数量 (${symbol})`}
          <button type="button" className="pt-unit" onClick={() => { setUnit(unit === "usdt" ? "coin" : "usdt"); setAmount(""); }}>
            按{unit === "usdt" ? "数量" : "金额"}
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
        <div><dt>数量</dt><dd>{qty > 0 ? n(qty, 6) : "—"} {symbol}</dd></div>
        {tpsl ? (
          <>
            <div><dt>止盈到账（估）</dt><dd>{qty > 0 && tp > 0 ? `${n(qty * tp * (1 - slipRate) * (1 - feeRate), 2)} USDT` : "—"}</dd></div>
            <div><dt>止损到账（估）</dt><dd>{qty > 0 && sl > 0 ? `${n(qty * sl * (1 - slipRate) * (1 - feeRate), 2)} USDT` : "—"}</dd></div>
          </>
        ) : (
          <div><dt>预估成交价</dt><dd>{qty > 0 ? n(execPx, d) : "—"}</dd></div>
        )}
        <div><dt>手续费 {(feeRate * 100).toFixed(3)}%</dt><dd>{qty > 0 ? n(fee, 4) : "—"}</dd></div>
        <div><dt>滑点 {(slipRate * 1e4).toFixed(0)}bp</dt><dd>{qty > 0 ? n(slip, 4) : "—"}</dd></div>
        {tpsl ? null : <div className="pt-total"><dt>{side === "buy" ? "合计支出" : "预计到账"}</dt><dd>{qty > 0 ? `${n(total, 2)} USDT` : "—"}</dd></div>}
      </dl>
      {check ? <p className="pt-warn">{check}</p> : null}
      <button type="button" className={`pt-submit ${side}`} disabled={busy || !(qty > 0) || !!check} onClick={submit}>
        {busy ? "提交中…" : tpsl ? `挂出${tp > 0 && sl > 0 ? " OCO " : ""}止盈止损 ${symbol}` : `纸面${side === "buy" ? "买入" : "卖出"} ${symbol}`}
      </button>
      {msg ? <p className={msg.ok ? "pt-ok" : "pt-warn"}>{msg.text}</p> : null}
      {tpsl ? (
        <p className="muted pt-note">
          触发后按市价卖出（含滑点与吃单费）；同时填两个价 = OCO，一边成交另一边自动撤销；同一分钟两边都触及时按止损处理。只用于手动纸面交易，自动策略不使用。
        </p>
      ) : null}
      <p className="muted pt-note">
        可用 {n(acct.availableCash)} USDT · 单笔≤{n(acct.limits.maxOrderUsdt, 0)} · 单币≤{n(acct.limits.maxPositionUsdt, 0)} · 只做现货多头
      </p>

      <div className="pt-box">
        <div className="pt-box-head"><b>{symbol} 持仓</b></div>
        {pos && pos.qty > 0 ? (
          <dl className="pt-est num">
            <div><dt>数量</dt><dd>{n(pos.qty, 6)}</dd></div>
            <div><dt>均价 / 现价</dt><dd>{n(pos.avgPrice, d)} / {n(pos.last, d)}</dd></div>
            <div><dt>市值</dt><dd>{n(pos.value)} USDT</dd></div>
            <div><dt>未实现盈亏</dt><dd className={tone(pos.unrealized)}>{signed(pos.unrealized)} ({signed((pos.last / pos.avgPrice - 1) * 100)}%)</dd></div>
            <div><dt>已实现（含费）</dt><dd className={tone(pos.realized)}>{signed(pos.realized)}</dd></div>
          </dl>
        ) : (
          <p className="muted pt-note">无持仓{pos && pos.realized ? ` · 已实现 ${signed(pos.realized)} USDT` : ""}</p>
        )}
        <dl className="pt-est num pt-acct">
          <div><dt>账户权益</dt><dd>{n(acct.equity)} USDT</dd></div>
          <div><dt>总盈亏</dt><dd className={tone(acct.pnl)}>{signed(acct.pnl)} ({signed(acct.pnlPct * 100)}%)</dd></div>
        </dl>
      </div>

      <div className="pt-box">
        <div className="pt-box-head pt-tabs">
          <button type="button" className={tab === "open" ? "is-on" : ""} onClick={() => setTab("open")}>挂单 {openHere.length}</button>
          <button type="button" className={tab === "history" ? "is-on" : ""} onClick={() => setTab("history")}>订单记录</button>
        </div>
        <ul className="pt-orders num">
          {(tab === "open" ? openHere : acct.orders).slice(0, 20).map((o) => (
            <li key={o.id}>
              <span className={o.side === "buy" ? "up" : "down"}>{o.side === "buy" ? "买" : "卖"}·{TYPE[o.type] ?? o.type}{o.ocoGroup ? "·OCO" : ""}</span>
              <span>
                {n(o.qty, 6)} @ {o.status !== "filled" && o.triggerPrice ? `触发 ${n(o.triggerPrice, pxDigits(o.triggerPrice))}` : n(o.fillPrice ?? o.limitPrice ?? 0, pxDigits(o.fillPrice ?? o.limitPrice ?? 1))}
              </span>
              <span className="muted">
                {STATUS[o.status]}{o.status === "filled" ? ` · 费 ${n(o.fee, 3)}` : ""}{o.reason && REASON[o.reason] && o.status !== "open" ? ` · ${REASON[o.reason]}` : ""}
              </span>
              <span className="muted">{hhmm(o.fillTs ?? o.ts)}</span>
              {o.status === "open" ? <button type="button" onClick={() => cancel(o.id)}>撤单</button> : null}
            </li>
          ))}
          {(tab === "open" ? openHere : acct.orders).length === 0 ? <li className="muted">暂无</li> : null}
        </ul>
      </div>
    </aside>
  );
}
