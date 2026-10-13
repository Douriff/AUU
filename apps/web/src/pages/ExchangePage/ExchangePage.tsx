import { useCallback, useEffect, useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { marketProvider, type ExchangeAccount, type ExchangeConfig, type ExchangeOrder } from "@/providers/HttpWsProvider";
import { errText } from "@/i18n/errors";
import { ExchangeGuide } from "./ExchangeGuide";

const NAMES: Record<string, string> = { binance: "Binance 币安", okx: "OKX 欧易" };

function newCoid(): string {
  const a = new Uint8Array(10);
  crypto.getRandomValues(a);
  return Array.from(a, (b) => (b % 36).toString(36)).join("") + Date.now().toString(36).slice(-6);
}

function fmt(n: number | null | undefined, d = 8): string {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  return Number(n).toLocaleString(undefined, { maximumFractionDigits: d });
}

/** /exchange — bind your own Binance / OKX key, view balances & orders, place/cancel spot orders. */
export function ExchangePage() {
  const [cfg, setCfg] = useState<ExchangeConfig | null>(null);
  const [accounts, setAccounts] = useState<ExchangeAccount[]>([]);
  const [err, setErr] = useState("");
  const [msg, setMsg] = useState("");
  const [ex, setEx] = useState("binance");
  const [bindForm, setBindForm] = useState({ apiKey: "", secret: "", passphrase: "" });
  const [busy, setBusy] = useState(false);
  const [balances, setBalances] = useState<{ asset: string; total: number; free: number; used: number }[]>([]);
  const [open, setOpen] = useState<ExchangeOrder[]>([]);
  const [hist, setHist] = useState<ExchangeOrder[]>([]);
  const [histSym, setHistSym] = useState("BTC/USDT");
  const [order, setOrder] = useState({ symbol: "BTC/USDT", side: "buy", type: "limit", amount: "", price: "" });
  const [coid, setCoid] = useState(newCoid());
  const [confirmOrder, setConfirmOrder] = useState(false);
  const [riskOk, setRiskOk] = useState(false);

  const bound = accounts.find((a) => a.exchange === ex);

  const loadCfg = useCallback(() => {
    marketProvider.exchangeConfig().then(setCfg).catch((e: unknown) => setErr(errText(e)));
  }, []);

  const loadAccounts = useCallback(() => {
    marketProvider.exchangeAccounts().then((r) => setAccounts(r.items)).catch(() => setAccounts([]));
  }, []);

  useEffect(() => {
    loadCfg();
  }, [loadCfg]);

  useEffect(() => {
    if (cfg?.access) loadAccounts();
  }, [cfg?.access, loadAccounts]);

  const refresh = useCallback(() => {
    if (!bound) return;
    setErr("");
    marketProvider.exchangeBalances(ex).then((r) => setBalances(r.items)).catch((e: unknown) => setErr(errText(e)));
    marketProvider.exchangeOpenOrders(ex).then((r) => setOpen(r.items)).catch((e: unknown) => setErr(errText(e)));
  }, [bound, ex]);

  useEffect(() => {
    setBalances([]);
    setOpen([]);
    setHist([]);
    refresh();
  }, [refresh]);

  const doBind = (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setErr("");
    setMsg("");
    marketProvider
      .exchangeBind(ex, { apiKey: bindForm.apiKey.trim(), secret: bindForm.secret.trim(), passphrase: bindForm.passphrase.trim() })
      .then((r) => {
        setMsg(`绑定成功：${NAMES[ex]} ${r.masked}`);
        setBindForm({ apiKey: "", secret: "", passphrase: "" });
        loadAccounts();
      })
      .catch((e2: unknown) => setErr(errText(e2)))
      .finally(() => setBusy(false));
  };

  const doUnbind = () => {
    if (!window.confirm(`确定解绑 ${NAMES[ex]}？本站会删除保存的 Key。之后请也去交易所删除这个 API Key。`)) return;
    marketProvider
      .exchangeUnbind(ex)
      .then(() => {
        setMsg("已解绑并删除本站保存的 Key。别忘了去交易所删除这个 API Key。");
        loadAccounts();
      })
      .catch((e: unknown) => setErr(errText(e)));
  };

  const loadHist = () => {
    marketProvider.exchangeHistory(ex, histSym || undefined).then((r) => setHist(r.items)).catch((e: unknown) => setErr(errText(e)));
  };

  const doPlace = () => {
    setBusy(true);
    setErr("");
    setMsg("");
    marketProvider
      .exchangePlace(ex, {
        clientOrderId: coid,
        symbol: order.symbol,
        side: order.side,
        type: order.type,
        amount: Number(order.amount),
        price: order.type === "limit" ? Number(order.price) : null,
      })
      .then((r) => {
        setMsg(`${r.duplicate ? "（同一笔订单，未重复提交）" : "下单成功"}：${r.order.symbol} ${r.order.side} 状态 ${r.order.status} 订单号 ${r.order.id}`);
        setCoid(newCoid());
        setConfirmOrder(false);
        refresh();
      })
      .catch((e: unknown) => {
        const text = errText(e);
        setErr(text);
        // keep the same clientOrderId only when the result is unknown, so a retry looks it up instead of resending
        if (!/未知/.test(text)) setCoid(newCoid());
        setConfirmOrder(false);
      })
      .finally(() => setBusy(false));
  };

  const doCancel = (o: ExchangeOrder) => {
    if (!window.confirm(`撤销订单 ${o.id}（${o.symbol}）？`)) return;
    marketProvider
      .exchangeCancel(ex, o.id, o.symbol)
      .then(() => {
        setMsg(`已撤单 ${o.id}`);
        refresh();
      })
      .catch((e: unknown) => setErr(errText(e)));
  };

  const primaryIp = cfg?.serverIps.primary ?? "156.227.238.242";
  const standbyIp = cfg?.serverIps.standby ?? "156.239.51.157";
  const amountN = Number(order.amount);
  const priceN = Number(order.price);
  const orderValid = amountN > 0 && (order.type === "market" || priceN > 0) && /^[A-Za-z0-9]{2,15}\/USDT$/i.test(order.symbol);

  return (
    <div className="shell-page settings-page pro-page exchange-page">
      <header className="pro-head">
        <h1>交易所接入（Binance / OKX 现货）</h1>
        <p>用你自己的 API Key 查看余额、挂单、历史订单，并手动下现货单。<a href="#guide">不会操作？看下方使用指南 ↓</a></p>
      </header>

      <div className="pro-alert exch-risk">
        <b>风险提示：</b>这里下的是<b>真实订单</b>，使用你在交易所的真实资金，可能亏损。只接受“读取 + 现货交易”权限、<b>禁止提现</b>的 API Key，并且必须设置 IP 白名单：
        <b> {primaryIp}</b>（东京主节点）、<b>{standbyIp}</b>（香港备用）。本站策略和模拟盘不会自动使用你的 Key。
      </div>

      {err ? <div className="pro-alert">{err}</div> : null}
      {msg ? <p className="td-note">{msg}</p> : null}

      {!cfg ? <p className="muted">加载中…</p> : !cfg.enabled || !cfg.access ? (
        <section className="settings-section exch-disabled" aria-disabled="true">
          <h2>功能暂未开放</h2>
          <p className="muted">
            {!cfg.enabled ? "交易所接入功能目前已关闭（管理员开关）。开放后这里会出现绑定入口。" : "交易所接入正在内测，你的账号暂未开通。"}
            你可以先阅读下方指南，提前在交易所创建好 API Key。
          </p>
        </section>
      ) : (
        <>
          {cfg.killSwitch ? <div className="pro-alert">管理员已开启紧急停止：暂停所有新订单，撤单仍可使用。</div> : null}
          <section className="settings-section">
            <h2>选择交易所</h2>
            <div className="exch-pick">
              {cfg.exchanges.map((x) => (
                <label key={x} className="pts-agree">
                  <input type="radio" name="exch" checked={ex === x} onChange={() => setEx(x)} />
                  <span>{NAMES[x] ?? x}{accounts.some((a) => a.exchange === x) ? " · 已绑定" : ""}</span>
                </label>
              ))}
            </div>
          </section>

          {!bound ? (
            <section className="settings-section">
              <h2>绑定 {NAMES[ex]}</h2>
              {!cfg.cryptoReady ? <div className="pro-alert">服务器加密密钥未配置，暂不能绑定。</div> : null}
              <form className="pts-form" onSubmit={doBind} autoComplete="off">
                <label className="pt-field">
                  <span>API Key</span>
                  <input value={bindForm.apiKey} onChange={(e) => setBindForm({ ...bindForm, apiKey: e.target.value })} maxLength={200} spellCheck={false} autoComplete="off" />
                </label>
                <label className="pt-field">
                  <span>Secret Key</span>
                  <input type="password" value={bindForm.secret} onChange={(e) => setBindForm({ ...bindForm, secret: e.target.value })} maxLength={200} autoComplete="new-password" />
                </label>
                {ex === "okx" ? (
                  <label className="pt-field">
                    <span>Passphrase（创建 OKX API 时自己设置的口令）</span>
                    <input type="password" value={bindForm.passphrase} onChange={(e) => setBindForm({ ...bindForm, passphrase: e.target.value })} maxLength={64} autoComplete="new-password" />
                  </label>
                ) : null}
                <label className="pts-agree">
                  <input type="checkbox" checked={riskOk} onChange={(e) => setRiskOk(e.target.checked)} />
                  <span>我已确认这个 Key 没有提现权限，已设置 IP 白名单 {primaryIp}、{standbyIp}，并了解真实交易风险。</span>
                </label>
                <button type="submit" className="td-submit" disabled={busy || !riskOk || !bindForm.apiKey || !bindForm.secret || (ex === "okx" && !bindForm.passphrase)}>
                  {busy ? "验证中…" : "验证并绑定"}
                </button>
              </form>
            </section>
          ) : (
            <>
              <section className="settings-section">
                <h2>{NAMES[ex]} · 已绑定</h2>
                <div className="set-row">
                  <div>
                    <b>{bound.masked}</b>
                    <p className="muted">
                      权限：读取 {bound.perms.read ? "✓" : "✗"} · 现货交易 {bound.perms.trade ? "✓" : "✗"} · 提现 ✗ · IP 白名单 {bound.perms.ipRestricted ? "✓" : "✗"}
                    </p>
                  </div>
                  <div>
                    <button type="button" className="td-link" onClick={refresh}>刷新</button>{" "}
                    <button type="button" className="td-link" onClick={doUnbind}>解绑</button>
                  </div>
                </div>
              </section>

              <section className="settings-section">
                <h2>余额</h2>
                {balances.length === 0 ? <p className="muted">没有余额或还在加载。</p> : (
                  <div className="pro-table-wrap">
                    <table className="pro-table">
                      <thead><tr><th>币种</th><th>总额</th><th>可用</th><th>冻结</th></tr></thead>
                      <tbody>
                        {balances.map((b) => (
                          <tr key={b.asset}><td>{b.asset}</td><td>{fmt(b.total)}</td><td>{fmt(b.free)}</td><td>{fmt(b.used)}</td></tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </section>

              <section className="settings-section">
                <h2>下单（现货）</h2>
                <p className="muted">单笔金额上限 {fmt(cfg.maxOrderUsdt, 2)} USDT。只支持 XXX/USDT。{!bound.perms.trade ? "这个 Key 没有交易权限，只能查看。" : ""}</p>
                <div className="exch-form">
                  <label className="pt-field"><span>交易对</span>
                    <input value={order.symbol} onChange={(e) => setOrder({ ...order, symbol: e.target.value.toUpperCase() })} maxLength={24} />
                  </label>
                  <label className="pt-field"><span>方向</span>
                    <select value={order.side} onChange={(e) => setOrder({ ...order, side: e.target.value })}>
                      <option value="buy">买入</option><option value="sell">卖出</option>
                    </select>
                  </label>
                  <label className="pt-field"><span>类型</span>
                    <select value={order.type} onChange={(e) => setOrder({ ...order, type: e.target.value })}>
                      <option value="limit">限价</option><option value="market">市价</option>
                    </select>
                  </label>
                  {order.type === "limit" ? (
                    <label className="pt-field"><span>价格（USDT）</span>
                      <input inputMode="decimal" value={order.price} onChange={(e) => setOrder({ ...order, price: e.target.value })} maxLength={20} />
                    </label>
                  ) : null}
                  <label className="pt-field"><span>数量（{order.symbol.split("/")[0] || "币"}）</span>
                    <input inputMode="decimal" value={order.amount} onChange={(e) => setOrder({ ...order, amount: e.target.value })} maxLength={20} />
                  </label>
                  {order.type === "limit" && orderValid ? <p className="muted exch-full">约 {fmt(amountN * priceN, 2)} USDT</p> : null}
                  {!confirmOrder ? (
                    <div className="exch-full"><button type="button" className="td-submit" disabled={!orderValid || busy || cfg.killSwitch || !bound.perms.trade} onClick={() => setConfirmOrder(true)}>下单</button></div>
                  ) : (
                    <div className="pro-alert exch-full">
                      确认在 {NAMES[ex]} 真实{order.side === "buy" ? "买入" : "卖出"} {order.amount} {order.symbol}
                      {order.type === "limit" ? `，价格 ${order.price}` : "（市价）"}？{" "}
                      <button type="button" className="td-submit" disabled={busy} onClick={doPlace}>{busy ? "提交中…" : "确认下单"}</button>{" "}
                      <button type="button" className="td-link" onClick={() => setConfirmOrder(false)}>取消</button>
                    </div>
                  )}
                </div>
              </section>

              <section className="settings-section">
                <h2>当前挂单</h2>
                {open.length === 0 ? <p className="muted">没有挂单。</p> : (
                  <div className="pro-table-wrap">
                    <table className="pro-table">
                      <thead><tr><th>交易对</th><th>方向</th><th>类型</th><th>价格</th><th>数量</th><th>已成交</th><th>状态</th><th></th></tr></thead>
                      <tbody>
                        {open.map((o) => (
                          <tr key={o.id}>
                            <td>{o.symbol}</td><td>{o.side === "buy" ? "买" : "卖"}</td><td>{o.type}</td><td>{fmt(o.price)}</td><td>{fmt(o.amount)}</td><td>{fmt(o.filled)}</td><td>{o.status}</td>
                            <td><button type="button" className="td-link" onClick={() => doCancel(o)}>撤单</button></td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </section>

              <section className="settings-section">
                <h2>历史订单</h2>
                <div className="exch-hist">
                  <label className="pt-field"><span>交易对{ex === "binance" ? "（币安必填）" : "（可留空）"}</span>
                    <input value={histSym} onChange={(e) => setHistSym(e.target.value.toUpperCase())} maxLength={24} />
                  </label>
                  <button type="button" className="td-link" onClick={loadHist}>查询</button>
                </div>
                {hist.length === 0 ? <p className="muted">点“查询”加载。</p> : (
                  <div className="pro-table-wrap">
                    <table className="pro-table">
                      <thead><tr><th>时间</th><th>交易对</th><th>方向</th><th>类型</th><th>均价</th><th>成交量</th><th>状态</th></tr></thead>
                      <tbody>
                        {hist.map((o) => (
                          <tr key={o.id}>
                            <td>{o.timestamp ? new Date(o.timestamp).toLocaleString() : "—"}</td><td>{o.symbol}</td><td>{o.side === "buy" ? "买" : "卖"}</td><td>{o.type}</td><td>{fmt(o.average ?? o.price)}</td><td>{fmt(o.filled)}</td><td>{o.status}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </section>
            </>
          )}
        </>
      )}

      <ExchangeGuide primaryIp={primaryIp} standbyIp={standbyIp} />
      <p className="muted"><Link to="/settings">‹ 返回设置</Link></p>
    </div>
  );
}
