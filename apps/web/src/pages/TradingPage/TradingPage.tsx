import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { CandleChart } from "@/components/chart/CandleChart";
import { WalletSignalCard } from "@/components/wallet/WalletSignalCard";
import { PriceCell } from "@/components/markets/PriceCell";
import { useMarkets } from "@/hooks/useMarkets";
import { marketProvider } from "@/providers/HttpWsProvider";
import type { Candle, MajorsCompare, SearchCoin, SearchCoinDetail, TradePosition, TradePreview } from "@/types/contracts";

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
  const [hits, setHits] = useState<SearchCoin[]>([]);
  const [searchNote, setSearchNote] = useState("");
  const [external, setExternal] = useState<SearchCoinDetail | null>(null);
  const [cex, setCex] = useState<MajorsCompare | null>(null);
  const [side, setSide] = useState<"buy" | "sell">("buy");
  const [amount, setAmount] = useState("0.1");
  const [sellPct, setSellPct] = useState(100);
  const [candles, setCandles] = useState<Candle[]>([]);
  const [preview, setPreview] = useState<TradePreview | null>(null);
  const [position, setPosition] = useState<TradePosition | null>(null);
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);

  const items = list?.items ?? [];
  const pooled = useMemo(() => {
    if (!mint) return null;
    return items.find((row) => row.mint === mint || row.symbol === mint || row.base === mint) ?? null;
  }, [items, mint]);

  const desk = useMemo(() => {
    if (pooled) {
      return {
        base: pooled.base,
        symbol: pooled.symbol,
        mint: pooled.mint || pooled.symbol,
        priceSol: pooled.price_sol,
        priceUsd: null as number | null,
        change: pooled.change_pct,
        mcapSol: pooled.market_cap_sol,
        progress: pooled.progress_pct,
        venue: pooled.progress_pct == null ? "" : "曲线",
        graduated: false,
        image: null as string | null,
        external: false,
      };
    }
    if (external && (external.mint === mint || external.trade_symbol === mint)) {
      return {
        base: external.symbol,
        symbol: external.trade_symbol,
        mint: external.mint,
        priceSol: external.price_sol,
        priceUsd: external.price_usd,
        change: external.change_24h,
        mcapSol: external.market_cap_sol,
        progress: external.progress_pct,
        venue: external.venue,
        graduated: external.graduated,
        image: external.image,
        external: true,
      };
    }
    return null;
  }, [pooled, external, mint]);

  const symbol = desk?.symbol ?? "";
  const notional = Number(amount);
  const overCap = side === "buy" && Number.isFinite(notional) && notional > 1 + 1e-9;

  useEffect(() => {
    setPreview(null);
    setPosition(null);
    setNotice("");
    setCex(null);
  }, [symbol]);

  useEffect(() => {
    const q = query.trim();
    if (q.length < 1) {
      setHits([]);
      setSearchNote("");
      return;
    }
    let stop = false;
    const timer = window.setTimeout(() => {
      void marketProvider
        .searchCoins(q)
        .then((data) => {
          if (stop) return;
          setHits(data.items);
          setSearchNote(data.error || "");
        })
        .catch((e) => {
          if (!stop) setSearchNote(e instanceof Error ? e.message : "搜索暂时不可用");
        });
    }, 300);
    return () => {
      stop = true;
      window.clearTimeout(timer);
    };
  }, [query]);

  useEffect(() => {
    if (!mint || pooled) {
      setExternal(null);
      return;
    }
    let stop = false;
    const tick = async () => {
      try {
        const coin = await marketProvider.getSearchCoin(mint);
        if (!stop) {
          setExternal(coin);
          if (coin.candles?.length) setCandles(coin.candles);
        }
      } catch (e) {
        if (!stop) setSearchNote(e instanceof Error ? e.message : "找不到这个代币");
      }
    };
    void tick();
    const id = window.setInterval(() => void tick(), 2500);
    return () => {
      stop = true;
      window.clearInterval(id);
    };
  }, [mint, pooled]);

  useEffect(() => {
    if (!symbol || desk?.external) return;
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
  }, [symbol, desk?.external]);

  useEffect(() => {
    if (!desk?.base) return;
    let stop = false;
    const tick = async () => {
      try {
        const next = await marketProvider.getMajorsCompare(desk.base, desk.priceUsd);
        if (!stop) setCex(next.listed ? next : null);
      } catch {
        if (!stop) setCex(null);
      }
    };
    void tick();
    const id = window.setInterval(() => void tick(), 4000);
    return () => {
      stop = true;
      window.clearInterval(id);
    };
  }, [desk?.base, desk?.priceUsd]);

  useEffect(() => {
    if (!symbol) return;
    let stop = false;
    const tick = async () => {
      try {
        const pos = await marketProvider.getTradePosition({ mint: desk?.mint, symbol });
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
  }, [symbol, desk?.mint]);

  useEffect(() => {
    if (!symbol) return;
    let stop = false;
    const tick = async () => {
      try {
        const next = await marketProvider.getTradePreview(
          side === "buy"
            ? { symbol, mint: desk?.mint, side, notional_sol: Number.isFinite(notional) && notional > 0 ? notional : 0.1 }
            : { symbol, mint: desk?.mint, side, sell_pct: sellPct }
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
  }, [symbol, desk?.mint, side, notional, sellPct]);

  const limits = preview?.limits ?? position?.limits;
  const qty = position?.qty ?? 0;
  const held = Boolean(limits?.held) || Math.abs(qty) > 1e-12;
  const atCap = Boolean(limits && limits.open_positions >= limits.max_open_positions && !held);
  const dayLoss = Boolean(limits?.day_loss_tripped);
  const buyBlocked =
    side === "buy" && (overCap || atCap || dayLoss || !Number.isFinite(notional) || notional <= 0 || Boolean(preview?.blocked));
  const sellBlocked = side === "sell" && (qty <= 1e-12 || Boolean(preview?.blocked));

  const openToken = (row: { mint?: string; symbol: string }) => {
    navigate(`/trade/${encodeURIComponent(row.mint || row.symbol)}`);
    setQuery("");
  };

  async function submit(nextSide: "buy" | "sell" = side, pct = sellPct) {
    if (!symbol || busy) return;
    setBusy(true);
    setNotice("");
    try {
      const result = await marketProvider.postTradeOrder(
        nextSide === "buy"
          ? { symbol, mint: desk?.mint, side: "buy", notional_sol: notional }
          : { symbol, mint: desk?.mint, side: "sell", sell_pct: pct }
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
            placeholder="搜索全部 pump.fun：名称 / 符号 / mint"
            type="search"
          />
        </label>
      </header>

      {!mint && (
        <section className="td-picker" aria-label="代币选择">
          <p className="td-note">{searchNote || err || "输入名称、符号或 mint，搜索全部 pump.fun 代币（含已毕业）。"}</p>
          <ul>
            {hits.map((row) => (
              <li key={row.mint}>
                <button type="button" onClick={() => openToken(row)}>
                  {row.image ? <img src={row.image} alt="" /> : <i aria-hidden="true">{row.symbol.slice(0, 1)}</i>}
                  <strong>{row.symbol}</strong>
                  <em>{row.name}</em>
                  <span className="num">
                    <PriceCell price={row.price_sol} />
                  </span>
                  <span className={`num ${tone(row.change_24h)}`}>{formatPct(row.change_24h)}</span>
                  <span className="num">{row.graduated ? row.venue : row.progress_pct == null ? "—" : `${row.progress_pct.toFixed(1)}%`}</span>
                </button>
              </li>
            ))}
          </ul>
        </section>
      )}

      {mint && !desk && <p className="td-note">{searchNote || err || "正在读取这个代币…"}</p>}

      {desk && (
        <>
          <section className="td-head" aria-label="报价">
            <div className="td-ident">
              {desk.image ? <img src={desk.image} alt="" /> : null}
              <div>
                <h2>{desk.base}</h2>
                <p>{desk.symbol}</p>
              </div>
            </div>
            <div>
              <span>价格</span>
              <strong className="num">
                <PriceCell price={desk.priceSol} /> SOL
                {desk.priceUsd != null ? <small> ${formatSol(desk.priceUsd, 4)}</small> : null}
              </strong>
            </div>
            <div>
              <span>{desk.external ? "24h 涨跌" : "涨跌"}</span>
              <strong className={`num ${tone(desk.change)}`}>{formatPct(desk.change)}</strong>
            </div>
            <div>
              <span>市值</span>
              <strong className="num">{formatSol(desk.mcapSol, 2)} SOL</strong>
            </div>
            <div>
              <span>{desk.graduated ? "成交场所" : "曲线进度"}</span>
              <strong className="num">
                {desk.graduated ? desk.venue : desk.progress == null ? "—" : `${desk.progress.toFixed(1)}%`}
              </strong>
            </div>
          </section>
          {cex?.listed && (
            <section className="td-cex" aria-label="交易所对照">
              <h3>交易所对照</h3>
              <ul>
                {cex.venues.map((row) => (
                  <li key={row.id}>
                    <span>{row.label}</span>
                    {row.status === "ok" && row.last != null ? (
                      <b className="num">
                        ${formatSol(row.last, 2)}
                        <small className={tone(row.vs_onchain)}>{formatPct(row.vs_onchain)}</small>
                      </b>
                    ) : (
                      <b className="muted">{row.status_label}</b>
                    )}
                  </li>
                ))}
              </ul>
            </section>
          )}

          <WalletSignalCard mint={desk.mint} priceSol={desk.priceSol} sellPct={sellPct} />

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
                  <dt>{preview?.impact_label === "估算" ? "估算冲击" : "曲线冲击"}</dt>
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
              {notice && (
                <p className="td-notice">
                  {notice}
                  {notice.includes("请先登录") && (
                    <>
                      {" "}
                      <Link to="/login">去登录</Link>
                    </>
                  )}
                </p>
              )}
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

      {mint && query && hits.length > 0 && (
        <ul className="td-suggest">
          {hits.slice(0, 6).map((row) => (
            <li key={row.mint}>
              <button type="button" onClick={() => openToken(row)}>
                {row.symbol}
                <em>{row.graduated ? row.venue : "曲线"}</em>
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
