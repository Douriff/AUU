import { useCallback, useEffect, useState } from "react";
import { marketProvider, type ExchangeAccount, type ExchangeConfig, type ExchangeOrder, type ExchangePosition } from "@/providers/HttpWsProvider";
import { errText } from "@/i18n/errors";

function newCoid(): string {
  const a = new Uint8Array(10);
  crypto.getRandomValues(a);
  return Array.from(a, (b) => (b % 36).toString(36)).join("") + Date.now().toString(36).slice(-6);
}
const fmt = (n: number | null | undefined, d = 6) => (n === null || n === undefined || Number.isNaN(Number(n)) ? "—" : Number(n).toLocaleString(undefined, { maximumFractionDigits: d }));
const normSym = (s: string) => {
  let x = s.trim().toUpperCase();
  if (!x.includes("/")) x += "/USDT";
  if (!x.includes(":")) x += ":USDT";
  return x;
};

type Props = { ex: string; exName: string; cfg: ExchangeConfig; bound: ExchangeAccount; onCfg: () => void; setErr: (s: string) => void; setMsg: (s: string) => void };

/** USDT-margined perpetuals: leverage / margin mode, positions, open/close, TP/SL, funding. */
export function FuturesPanel({ ex, exName, cfg, bound, onCfg, setErr, setMsg }: Props) {
  const [ackChecks, setAckChecks] = useState([false, false, false]);
  const [positions, setPositions] = useState<ExchangePosition[]>([]);
  const [open, setOpen] = useState<ExchangeOrder[]>([]);
  const [sym, setSym] = useState("BTC/USDT:USDT");
  const [lev, setLev] = useState(Math.min(3, cfg.maxLeverage));
  const [mode, setMode] = useState("isolated");
  const [funding, setFunding] = useState<{ fundingRate?: number | null; nextFundingTime?: number | null; markPrice?: number | null } | null>(null);
  const [o, setO] = useState({ side: "buy", type: "limit", amount: "", price: "", tp: "", sl: "", reduceOnly: false });
  const [coid, setCoid] = useState(newCoid());
  const [confirm, setConfirm] = useState(false);
  const [typed, setTyped] = useState("");
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(() => {
    if (!cfg.futuresAck) return;
    marketProvider.exchangeFuturesPositions(ex).then((r) => setPositions(r.items)).catch((e: unknown) => setErr(errText(e)));
    marketProvider.exchangeFuturesOpen(ex).then((r) => setOpen(r.items)).catch(() => undefined);
  }, [cfg.futuresAck, ex, setErr]);
  useEffect(() => refresh(), [refresh]);

  const loadFunding = useCallback(() => {
    if (!cfg.futuresAck) return;
    marketProvider.exchangeFuturesFunding(ex, normSym(sym)).then(setFunding).catch(() => setFunding(null));
  }, [cfg.futuresAck, ex, sym]);
  useEffect(() => loadFunding(), [loadFunding]);

  if (!cfg.futuresAck) {
    return (
      <section className="settings-section fut-ack">
        <h2>合约风险确认（首次使用）</h2>
        <div className="pro-alert exch-risk">
          永续合约是<b>高风险</b>产品：使用杠杆后，价格小幅反向波动就可能导致<b>强制平仓（爆仓）</b>，逐仓模式可能亏光该仓位保证金，全仓模式可能亏掉账户里全部合约保证金。还要支付/收取资金费率。
        </div>
        {["我明白合约可能爆仓，亏损可能超过预期，甚至亏光保证金。", `我明白本站最高只允许 ${cfg.maxLeverage} 倍杠杆，单笔名义价值上限 ${fmt(cfg.maxFuturesUsdt, 2)} USDT。`, "我会自己设置止损，盈亏由我自己承担，本站不提供投资建议。"].map((t, i) => (
          <label key={i} className="pts-agree">
            <input type="checkbox" checked={ackChecks[i]} onChange={(e) => setAckChecks(ackChecks.map((v, j) => (j === i ? e.target.checked : v)))} />
            <span>{t}</span>
          </label>
        ))}
        <button type="button" className="td-submit" disabled={!ackChecks.every(Boolean)} onClick={() => marketProvider.exchangeFuturesAck().then(onCfg).catch((e: unknown) => setErr(errText(e)))}>
          我已阅读并确认，开通合约
        </button>
      </section>
    );
  }

  const amountN = Number(o.amount);
  const valid = amountN > 0 && (o.type === "market" || Number(o.price) > 0);
  const opening = !o.reduceOnly;

  const place = () => {
    setBusy(true);
    setErr("");
    marketProvider
      .exchangeFuturesPlace(ex, {
        clientOrderId: coid, symbol: normSym(sym), side: o.side, type: o.type, amount: amountN,
        price: o.type === "limit" ? Number(o.price) : null, reduceOnly: o.reduceOnly,
        takeProfit: o.tp ? Number(o.tp) : null, stopLoss: o.sl ? Number(o.sl) : null, confirmed: true,
      })
      .then((r) => {
        const extra = r.tpsl.map((t) => `${t.kind === "takeProfit" ? "止盈" : "止损"}${t.ok ? "已设置" : "失败：" + (t.message ?? "")}`).join("，");
        setMsg(`${r.duplicate ? "（同一笔订单，未重复提交）" : "合约下单成功"}：${r.order.symbol} ${r.order.side} 状态 ${r.order.status}${extra ? "；" + extra : ""}`);
        setCoid(newCoid());
        refresh();
      })
      .catch((e: unknown) => {
        const t = errText(e);
        setErr(t);
        if (!/未知/.test(t)) setCoid(newCoid());
      })
      .finally(() => { setBusy(false); setConfirm(false); setTyped(""); });
  };

  const closePos = (p: ExchangePosition) => {
    if (!window.confirm(`以市价平掉 ${p.symbol} ${p.side === "long" ? "多" : "空"}仓 ${p.contracts} 张？`)) return;
    marketProvider.exchangeFuturesClose(ex, p.symbol, newCoid()).then(() => { setMsg(`已提交平仓 ${p.symbol}`); refresh(); }).catch((e: unknown) => setErr(errText(e)));
  };
  const cancel = (x: ExchangeOrder) => {
    if (!window.confirm(`撤销合约订单 ${x.id}？`)) return;
    marketProvider.exchangeFuturesCancel(ex, x.id, x.symbol).then(() => { setMsg(`已撤单 ${x.id}`); refresh(); }).catch((e: unknown) => setErr(errText(e)));
  };

  return (
    <>
      {!bound.perms.futures && ex === "binance" ? <div className="pro-alert">这个币安 Key 没有开启「合约」权限。请在币安 API 管理里勾选「允许合约」（Enable Futures）后解绑重绑。</div> : null}
      <section className="settings-section">
        <h2>合约设置（USDT 本位永续 · {exName}）</h2>
        <div className="exch-form">
          <label className="pt-field"><span>合约</span><input value={sym} onChange={(e) => setSym(e.target.value.toUpperCase())} maxLength={32} onBlur={loadFunding} /></label>
          <label className="pt-field"><span>保证金模式</span>
            <select value={mode} onChange={(e) => setMode(e.target.value)}><option value="isolated">逐仓（推荐）</option><option value="cross">全仓</option></select>
          </label>
          <label className="pt-field"><span>杠杆（最高 {cfg.maxLeverage}x）</span>
            <select value={lev} onChange={(e) => setLev(Number(e.target.value))}>
              {Array.from({ length: cfg.maxLeverage }, (_, i) => i + 1).map((n) => <option key={n} value={n}>{n}x</option>)}
            </select>
          </label>
          <div><button type="button" className="td-link" onClick={() => marketProvider.exchangeFuturesLeverage(ex, { symbol: normSym(sym), leverage: lev, marginMode: mode }).then((r) => setMsg(`已设置 ${r.symbol} ${r.marginMode === "cross" ? "全仓" : "逐仓"} ${r.leverage}x`)).catch((e: unknown) => setErr(errText(e)))}>应用杠杆/模式</button></div>
        </div>
        <p className="muted">
          资金费率：{funding?.fundingRate !== undefined && funding?.fundingRate !== null ? `${(Number(funding.fundingRate) * 100).toFixed(4)}%` : "—"}
          {funding?.nextFundingTime ? ` · 下次结算 ${new Date(funding.nextFundingTime).toLocaleString()}` : ""}
          {funding?.markPrice ? ` · 标记价 ${fmt(funding.markPrice, 4)}` : ""}（正数=多头付给空头）
        </p>
      </section>

      <section className="settings-section">
        <h2>合约下单</h2>
        <p className="muted">单笔名义价值上限 {fmt(cfg.maxFuturesUsdt, 2)} USDT（开仓）。{ex === "okx" ? "OKX 数量单位是「张」，每张面值见合约说明（BTC 为 0.01 BTC）。" : "币安数量单位是币（如 BTC）。"}</p>
        <div className="exch-form">
          <label className="pt-field"><span>方向</span>
            <select value={o.side} onChange={(e) => setO({ ...o, side: e.target.value })}><option value="buy">{o.reduceOnly ? "买入（平空）" : "开多 / 买入"}</option><option value="sell">{o.reduceOnly ? "卖出（平多）" : "开空 / 卖出"}</option></select>
          </label>
          <label className="pt-field"><span>类型</span>
            <select value={o.type} onChange={(e) => setO({ ...o, type: e.target.value })}><option value="limit">限价</option><option value="market">市价</option></select>
          </label>
          {o.type === "limit" ? <label className="pt-field"><span>价格</span><input inputMode="decimal" value={o.price} onChange={(e) => setO({ ...o, price: e.target.value })} maxLength={20} /></label> : null}
          <label className="pt-field"><span>数量{ex === "okx" ? "（张）" : ""}</span><input inputMode="decimal" value={o.amount} onChange={(e) => setO({ ...o, amount: e.target.value })} maxLength={20} /></label>
          {opening ? <label className="pt-field"><span>止盈价（可选）</span><input inputMode="decimal" value={o.tp} onChange={(e) => setO({ ...o, tp: e.target.value })} maxLength={20} /></label> : null}
          {opening ? <label className="pt-field"><span>止损价（建议设置）</span><input inputMode="decimal" value={o.sl} onChange={(e) => setO({ ...o, sl: e.target.value })} maxLength={20} /></label> : null}
          <label className="pts-agree exch-full"><input type="checkbox" checked={o.reduceOnly} onChange={(e) => setO({ ...o, reduceOnly: e.target.checked })} /><span>只减仓（reduceOnly，用于平仓，不会开新仓）</span></label>
          <div className="exch-full">
            <button type="button" className="td-submit" disabled={!valid || busy || (cfg.killSwitch && opening)} onClick={() => setConfirm(true)}>{o.reduceOnly ? "平仓下单" : "开仓下单"}</button>
            {cfg.killSwitch && opening ? <span className="muted"> 紧急停止中：只能平仓</span> : null}
          </div>
        </div>
      </section>

      {confirm ? (
        <div className="modal-backdrop" role="presentation">
          <div className="modal-card fut-modal" role="dialog" aria-modal="true" aria-label="合约下单确认">
            <h2>确认合约{o.reduceOnly ? "平仓" : "开仓"}（真实资金）</h2>
            <p>{exName} · {normSym(sym)} · {o.side === "buy" ? "买入/做多" : "卖出/做空"} · {o.type === "limit" ? `限价 ${o.price}` : "市价"} · 数量 {o.amount}{ex === "okx" ? " 张" : ""}</p>
            {opening ? <p>杠杆/模式以交易所当前设置为准（你选择的是 {lev}x {mode === "cross" ? "全仓" : "逐仓"}，需先点「应用」）。止盈 {o.tp || "无"} · 止损 {o.sl || "无"}</p> : null}
            <div className="pro-alert exch-risk">杠杆交易可能被强制平仓，亏损可能很快且超过预期。{opening && !o.sl ? <b>你没有设置止损。</b> : null}</div>
            <label className="pt-field"><span>请输入「确认」两个字继续</span><input value={typed} onChange={(e) => setTyped(e.target.value)} maxLength={4} /></label>
            <div className="fut-modal-actions">
              <button type="button" className="td-link" onClick={() => { setConfirm(false); setTyped(""); }}>取消</button>
              <button type="button" className="td-submit" disabled={typed.trim() !== "确认" || busy} onClick={place}>{busy ? "提交中…" : "确认下单"}</button>
            </div>
          </div>
        </div>
      ) : null}

      <section className="settings-section">
        <h2>合约持仓 <button type="button" className="td-link" onClick={refresh}>刷新</button></h2>
        {positions.length === 0 ? <p className="muted">没有持仓。</p> : (
          <div className="pro-table-wrap">
            <table className="pro-table">
              <thead><tr><th>合约</th><th>方向</th><th>数量</th><th>开仓价</th><th>标记价</th><th>强平价</th><th>未实现盈亏</th><th>杠杆/模式</th><th></th></tr></thead>
              <tbody>
                {positions.map((p) => (
                  <tr key={p.symbol + p.side}>
                    <td>{p.symbol}</td><td className={p.side === "long" ? "up" : "down"}>{p.side === "long" ? "多" : "空"}</td><td>{fmt(p.contracts)}</td>
                    <td>{fmt(p.entryPrice, 4)}</td><td>{fmt(p.markPrice, 4)}</td><td className="down">{fmt(p.liquidationPrice, 4)}</td>
                    <td className={Number(p.unrealizedPnl) >= 0 ? "up" : "down"}>{fmt(p.unrealizedPnl, 4)}</td>
                    <td>{p.leverage ? `${p.leverage}x` : "—"} {p.marginMode === "cross" ? "全仓" : p.marginMode === "isolated" ? "逐仓" : ""}</td>
                    <td><button type="button" className="td-link" onClick={() => closePos(p)}>市价平仓</button></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <section className="settings-section">
        <h2>合约挂单（含止盈止损）</h2>
        {open.length === 0 ? <p className="muted">没有挂单。</p> : (
          <div className="pro-table-wrap">
            <table className="pro-table">
              <thead><tr><th>合约</th><th>方向</th><th>类型</th><th>价格</th><th>数量</th><th>状态</th><th></th></tr></thead>
              <tbody>
                {open.map((x) => (
                  <tr key={x.id}><td>{x.symbol}</td><td>{x.side === "buy" ? "买" : "卖"}</td><td>{x.type}</td><td>{fmt(x.price, 4)}</td><td>{fmt(x.amount)}</td><td>{x.status}</td>
                    <td><button type="button" className="td-link" onClick={() => cancel(x)}>撤单</button></td></tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </>
  );
}
