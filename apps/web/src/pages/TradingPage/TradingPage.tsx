import { useEffect, useMemo, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { CandleChart } from "@/components/chart/CandleChart";
import { PriceCell } from "@/components/markets/PriceCell";
import { useMarkets } from "@/hooks/useMarkets";
import { marketProvider } from "@/providers/HttpWsProvider";
import type { Candle, MarketItem, TradePosition, TradePreview } from "@/types/contracts";

const BUY_AMOUNTS = [0.1, 0.25, 0.5, 1];
const SELL_PCTS = [25, 50, 100];

function formatPct(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n)) return "—";
  const pct = n * 100;
  const body = `${Math.abs(pct).toFixed(2)}%`;
  if (pct > 0) return `+${body}`;
  if (pct < 0) return `-${body}`;
  return body;
}

function formatSol(n: number | null | undefined, digits = 4): string {
  if (n == null || !Number.isFinite(n)) return "—";
  const a = Math.abs(n);
  if (a >= 1e6) return `${(n / 1e6).toFixed(2)}M`;
  if (a >= 1e3) return `${(n / 1e3).toFixed(2)}K`;
  if (a >= 1) return n.toFixed(Math.min(digits, 4));
  if (a === 0) return "0";
  return n.toFixed(digits);
}

function tone(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n) || n === 0) return "flat";
  return n > 0 ? "up" : "down";
}

function clock(ts: number): string {
  if (!ts) return "—";
  return new Intl.DateTimeFormat("en-GB", {
    timeZone: "Asia/Shanghai",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(new Date(ts));
}

export function TradingPage() {
  const { mint: mintParam } = useParams();
  const navigate = useNavigate();
  const mint = mintParam ? decodeURIComponent(mintParam) : "";
  const { list, err } = useMarkets(2500);
  const [query, setQuery] = useState("");
  const [side, setSide] = useState<"buy" | "sell">("buy");
  const [amount, setAmount] = useState("0.1");
  const [sellPct, setSellPct] = useState(100);
  const [candles, setCandles] = useState<Candle[]>([]);
  const [preview, setPreview] = useState<TradePreview | null>(null);
  const [position, setPosition] = useState<TradePosition | null>(null);
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);

  const items = list?.items ?? [];
  const selected = useMemo(() => {
    if (!mint) return null;
    return items.find((row) => row.mint === mint || row.symbol === mint || row.base === mint) ?? null;
  }, [items, mint]);

  const matches = useMemo(() => {
    const q = query.trim().toLowerCase();
    const rows = q
      ? items.filter(
          (row) =>
            row.base.toLowerCase().includes(q) ||
            row.symbol.toLowerCase().includes(q) ||
            row.mint.toLowerCase().includes(q)
        )
      : items;
    return rows.slice(0, 14);
  }, [items, query]);

  const symbol = selected?.symbol ?? "";
  const notional = Number(amount);
  const overCap = side === "buy" && Number.isFinite(notional) && notional > 1 + 1e-9;

  useEffect(() => {
    setPreview(null);
    setPosition(null);
    setCandles([]);
    setNotice("");
  }, [symbol]);

  useEffect(() => {
    if (!symbol) return;
    let stop = false;
    const tick = async () => {
      try {
        const next = await marketProvider.getCandles(symbol, "1m");
        if (!stop) setCandles(next);
      } catch {
        /* keep the last candles */
      }
    };
    void tick();
    const id = window.setInterval(() => void tick(), 2500);
    return () => {
      stop = true;
      window.clearInterval(id);
    };
  }, [symbol]);

  useEffect(() => {
    if (!symbol) return;
    let stop = false;
    const tick = async () => {
      try {
        const pos = await marketProvider.getTradePosition({ symbol });
        if (!stop) setPosition(pos);
      } catch (e) {
        if (!stop) setNotice(e instanceof Error ? e.message : "仓位读取失败");
      }
    };
    void tick();
    const id = window.setInterval(() => void tick(), 2500);
    return () => {
      stop = true;
      window.clearInterval(id);
    };
  }, [symbol]);

  useEffect(() => {
    if (!symbol) return;
    let stop = false;
    const tick = async () => {
      try {
        const next = await marketProvider.getTradePreview(
          side === "buy"
            ? { symbol, side, notional_sol: Number.isFinite(notional) && notional > 0 ? notional : 0.1 }
            : { symbol, side, sell_pct: sellPct }
        );
        if (!stop) setPreview(next);
      } catch (e) {
        if (!stop) setNotice(e instanceof Error ? e.message : "预览失败");
      }
    };
    void tick();
    const id = window.setInterval(() => void tick(), 2500);
    return () => {
      stop = true;
      window.clearInterval(id);
    };
  }, [symbol, side, notional, sellPct]);

  const limits = preview?.limits ?? position?.limits;
  const qty = position?.qty ?? 0;
  const held = Boolean(limits?.held) || Math.abs(qty) > 1e-12;
  const atCap = Boolean(limits && limits.open_positions >= limits.max_open_positions && !held);
  const dayLoss = Boolean(limits?.day_loss_tripped);
  const buyBlocked =
    side === "buy" && (overCap || atCap || dayLoss || !Number.isFinite(notional) || notional <= 0 || Boolean(preview?.blocked));
  const sellBlocked = side === "sell" && (qty <= 1e-12 || Boolean(preview?.blocked));

  const openToken = (row: MarketItem) => {
    navigate(`/trade/${encodeURIComponent(row.mint || row.symbol)}`);
  };

  async function submit(nextSide: "buy" | "sell" = side, pct = sellPct) {
    if (!symbol || busy) return;
    setBusy(true);
    setNotice("");
    try {
      const result = await marketProvider.postTradeOrder(
        nextSide === "buy"
          ? { symbol, side: "buy", notional_sol: notional }
          : { symbol, side: "sell", sell_pct: pct }
      );
      setPosition(result);
      if (result.reject && (result.reject.notes || (result.reject.tags || []).length)) {
        setNotice(result.reject.notes || (result.reject.tags || []).join(" "));
      } else if (Array.isArray(result.submitted) && result.submitted.length) {
        setNotice(nextSide === "buy" ? "纸面买入已成交" : "纸面卖出已成交");
      }
    } catch (e) {
      setNotice(e instanceof Error ? e.message : "下单失败");
    } finally {
      setBusy(false);
    }
  }

  const blockCopy = overCap
    ? "单笔上限 1 SOL"
    : atCap
      ? "持仓上限 10"
      : dayLoss && side === "buy"
        ? "日亏已达 4.5%，买入已停止"
        : preview?.block_message || "";

  return (
    <div className="trade-desk">
      <header className="td-top">
        <div className="td-title">
          <h1>交易</h1>
          <span className="td-badge paper">PAPER</span>
          <span className="td-badge live">LIVE OFF</span>
        </div>
        <label className="td-search">
          <span className="sr-only">搜索代币</span>
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="搜索名称 / 符号 / mint"
            type="search"
          />
        </label>
      </header>

      {!mint && (
        <section className="td-picker" aria-label="代币选择">
          <p className="td-note">{err || "从市场列表选一个代币，进入纸面交易。"}</p>
          <ul>
            {matches.map((row) => (
              <li key={row.symbol}>
                <button type="button" onClick={() => openToken(row)}>
                  <strong>{row.base}</strong>
                  <em>{row.symbol}</em>
                  <span className="num">
                    <PriceCell price={row.price_sol} />
                  </span>
                  <span className={`num ${tone(row.change_pct)}`}>{formatPct(row.change_pct)}</span>
                  <span className="num">{row.progress_pct == null ? "—" : `${row.progress_pct.toFixed(1)}%`}</span>
                </button>
              </li>
            ))}
          </ul>
        </section>
      )}

      {mint && !selected && (
        <p className="td-note">{items.length ? "找不到这个代币" : err || "正在读取市场…"}</p>
      )}

      {selected && (
        <>
          <section className="td-head" aria-label="报价">
            <div>
              <h2>{selected.base}</h2>
              <p>{selected.symbol}</p>
            </div>
            <div>
              <span>价格</span>
              <strong className="num">
                <PriceCell price={selected.price_sol} /> SOL
              </strong>
            </div>
            <div>
              <span>涨跌</span>
              <strong className={`num ${tone(selected.change_pct)}`}>{formatPct(selected.change_pct)}</strong>
            </div>
            <div>
              <span>市值</span>
              <strong className="num">{formatSol(selected.market_cap_sol, 2)} SOL</strong>
            </div>
            <div>
              <span>曲线进度</span>
              <strong className="num">
                {selected.progress_pct == null ? "—" : `${selected.progress_pct.toFixed(1)}%`}
              </strong>
            </div>
          </section>

          <div className="td-grid">
            <div className="td-chart">
              <CandleChart candles={candles} signals={[]} fills={[]} />
            </div>
            <form
              className="td-ticket"
              onSubmit={(event) => {
                event.preventDefault();
                if (side === "buy" ? buyBlocked : sellBlocked) return;
                void submit();
              }}
            >
              <div className="td-sides" role="tablist" aria-label="方向">
                <button type="button" className={side === "buy" ? "is-on buy" : ""} onClick={() => setSide("buy")}>
                  买入
                </button>
                <button type="button" className={side === "sell" ? "is-on sell" : ""} onClick={() => setSide("sell")}>
                  卖出
                </button>
              </div>

              {side === "buy" ? (
                <>
                  <label className="td-field">
                    金额 (SOL)
                    <input
                      inputMode="decimal"
                      value={amount}
                      onChange={(e) => setAmount(e.target.value)}
                    />
                  </label>
                  <div className="td-quick">
                    {BUY_AMOUNTS.map((n) => (
                      <button key={n} type="button" className={Number(amount) === n ? "is-on" : ""} onClick={() => setAmount(String(n))}>
                        {n}
                      </button>
                    ))}
                  </div>
                </>
              ) : (
                <div className="td-quick">
                  {SELL_PCTS.map((n) => (
                    <button key={n} type="button" className={sellPct === n ? "is-on" : ""} onClick={() => setSellPct(n)}>
                      {n}%
                    </button>
                  ))}
                </div>
              )}

              <dl className="td-preview">
                <div>
                  <dt>曲线冲击</dt>
                  <dd className="num">{preview ? `${preview.impact_bps.toFixed(1)} bps` : "—"}</dd>
                </div>
                <div>
                  <dt>费用</dt>
                  <dd className="num">
                    {preview ? `${preview.fee_bps.toFixed(0)} bps · ${formatSol(preview.fee_sol, 6)} SOL` : "—"}
                  </dd>
                </div>
                <div>
                  <dt>预计成交</dt>
                  <dd className="num">
                    {preview
                      ? `${formatSol(preview.expected_qty, 4)} @ ${formatSol(preview.expected_price, 8)}`
                      : "—"}
                  </dd>
                </div>
              </dl>
              <p className="td-limits">单笔 ≤ 1 SOL · 同时持仓 ≤ 10 · 日亏 4.5% 停止买入</p>
              {blockCopy && <p className="td-block">{blockCopy}</p>}
              {notice && <p className="td-notice">{notice}</p>}
              <button className={`td-submit ${side}`} type="submit" disabled={busy || (side === "buy" ? buyBlocked : sellBlocked)}>
                {busy ? "提交中…" : side === "buy" ? "纸面买入" : "纸面卖出"}
              </button>
            </form>
          </div>

          <section className="td-lower">
            <div className="td-pos">
              <header>
                <h3>我的仓位</h3>
                <button
                  type="button"
                  disabled={busy || qty <= 1e-12}
                  onClick={() => {
                    setSide("sell");
                    setSellPct(100);
                    void submit("sell", 100);
                  }}
                >
                  市价平仓
                </button>
              </header>
              {qty <= 1e-12 ? (
                <p className="td-note">这个代币没有纸面仓位</p>
              ) : (
                <dl>
                  <div>
                    <dt>数量</dt>
                    <dd className="num">{formatSol(qty, 4)}</dd>
                  </div>
                  <div>
                    <dt>成本</dt>
                    <dd className="num">{formatSol(position?.entry_price, 8)}</dd>
                  </div>
                  <div>
                    <dt>名义</dt>
                    <dd className="num">{formatSol(position?.notional_sol, 4)} SOL</dd>
                  </div>
                  <div>
                    <dt>浮动盈亏</dt>
                    <dd className={`num ${tone(position?.upnl)}`}>{formatSol(position?.upnl, 6)} SOL</dd>
                  </div>
                </dl>
              )}
            </div>
            <div className="td-fills">
              <h3>最近成交</h3>
              {(position?.fills.length ?? 0) === 0 ? (
                <p className="td-note">还没有这笔代币的纸面成交</p>
              ) : (
                <ul>
                  {[...(position?.fills ?? [])].reverse().map((fill) => (
                    <li key={`${fill.ts}-${fill.side}-${fill.qty}`}>
                      <span>{clock(fill.ts)}</span>
                      <span className={fill.side === "buy" ? "up" : "down"}>{fill.side === "buy" ? "买入" : "卖出"}</span>
                      <span className="num">{formatSol(fill.qty, 4)}</span>
                      <span className="num">{formatSol(fill.price, 8)}</span>
                      <span>{fill.source === "manual" ? "手动" : fill.source}</span>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </section>
        </>
      )}

      {mint && query && matches.length > 0 && selected && (
        <ul className="td-suggest">
          {matches.slice(0, 6).map((row) => (
            <li key={row.symbol}>
              <button type="button" onClick={() => openToken(row)}>
                {row.base}
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
