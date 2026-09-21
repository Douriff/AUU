import { useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { useDataSource } from "@/hooks/useDataSource";
import { marketProvider } from "@/providers/HttpWsProvider";
import type {
  BookSnapshot,
  Fill,
  OrderSide,
  RejectOut,
  RiskOut,
  SymbolInfo,
} from "@/types/contracts";
import { DATA_SOURCES, DEFAULT_SYMBOL, truncateMint } from "@/venue";

type TapeKind = "fill" | "reject" | "risk" | "signal" | "trading_state";

interface TapeRow {
  id: number;
  kind: TapeKind;
  ts: number;
  text: string;
}

function fmt(n: number, digits = 6): string {
  if (!Number.isFinite(n)) return "—";
  return n.toPrecision(digits);
}

export function TradePage() {
  const { dataSource, setDataSource } = useDataSource();
  const [symbols, setSymbols] = useState<SymbolInfo[]>([]);
  const [symbol, setSymbol] = useState(DEFAULT_SYMBOL);
  const [notional, setNotional] = useState("0.1");
  const [side, setSide] = useState<OrderSide>("buy");
  const [wideSpread, setWideSpread] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [lastRisk, setLastRisk] = useState<RiskOut | null>(null);
  const [lastReject, setLastReject] = useState<RejectOut | null>(null);
  const [lastFills, setLastFills] = useState<Fill[]>([]);
  const [lastPath, setLastPath] = useState<"two-step" | "pipeline" | "">("");
  const [book, setBook] = useState<BookSnapshot | null>(null);
  const [wsStatus, setWsStatus] = useState("closed");
  const [tape, setTape] = useState<TapeRow[]>([]);
  const tapeId = useRef(0);
  const symbolRef = useRef(symbol);
  symbolRef.current = symbol;

  const pushTape = (kind: TapeKind, ts: number, text: string) => {
    tapeId.current += 1;
    const row: TapeRow = { id: tapeId.current, kind, ts, text };
    setTape((prev) => [row, ...prev].slice(0, 40));
  };

  useEffect(() => {
    marketProvider.listSymbols().then(setSymbols).catch(() => undefined);
  }, []);

  useEffect(() => {
    const unsub = marketProvider.connect({
      onStatus: setWsStatus,
      onBook: (b) => {
        if (b.symbol !== symbolRef.current) return;
        setBook(b);
      },
      onFill: (f) => {
        if (f.symbol && f.symbol !== symbolRef.current) return;
        pushTape(
          "fill",
          f.ts,
          `px=${fmt(f.price)} qty=${fmt(f.qty)} fee=${fmt(f.fee ?? 0)} slip=${fmt(f.slippage_bps ?? 0, 4)}bps tag=${f.tag ?? ""}`
        );
      },
      onReject: (r) => {
        if (r.symbol && r.symbol !== symbolRef.current) return;
        pushTape("reject", r.ts, `${(r.tags ?? []).join(",") || "reject"} ${r.notes ?? ""}`);
      },
      onRisk: (r) => {
        if (r.symbol && r.symbol !== symbolRef.current) return;
        pushTape(
          "risk",
          r.t,
          `${r.risk.allow ? "ALLOW" : "DENY"} ${(r.risk.tags ?? []).join(",") || "no tags"} ${r.risk.notes ?? ""}`
        );
      },
      onSignal: (s) => {
        if (s.symbol && s.symbol !== symbolRef.current) return;
        pushTape("signal", s.t ?? Date.now(), `${s.side} ${s.reason ?? ""}`);
      },
      onTradingState: (t) => {
        pushTape("trading_state", t.ts ?? Date.now(), `${t.state}${t.reason ? ` · ${t.reason}` : ""}`);
      },
    });
    return unsub;
  }, []);

  useEffect(() => {
    // Book snapshot for mid. Paper fill/reject/risk/signal arrive via hub
    // without mock-channel subscribe (avoids demo-momentum tape noise).
    marketProvider.subscribe("book", symbol);
  }, [symbol]);

  const mid = book?.mid;
  const nom = useMemo(() => {
    const n = Number(notional);
    return Number.isFinite(n) ? n : NaN;
  }, [notional]);

  async function buildCtx() {
    const snap = await marketProvider.getBook(symbol).catch(() => book);
    const candles = snap ? null : await marketProvider.getCandles(symbol, "1m");
    const px = snap?.mid ?? (candles && candles.length ? candles[candles.length - 1].c : 1);
    return {
      symbol,
      ts: Date.now(),
      account: { equity: 10_000, day_pnl: 0 },
      liquidity: {
        spread_bps: wideSpread ? 200 : (snap?.spread_bps ?? 20),
        adv_usd: 100_000,
      },
      book: snap ? { bids: snap.bids, asks: snap.asks } : undefined,
      tick: { mid: px },
      meta: { venue: "pump.fun", mint: symbol },
    };
  }

  async function submitTwoStep(nextSide: OrderSide) {
    setSide(nextSide);
    setBusy(true);
    setError("");
    setLastReject(null);
    setLastFills([]);
    setLastPath("two-step");
    try {
      if (!(nom > 0)) throw new Error("notional must be > 0");
      const ctx = await buildCtx();
      const signal = {
        side: nextSide === "buy" ? ("long" as const) : ("short" as const),
        strength: 0.6,
        reason: "trade-ui",
        tags: ["paper"],
      };
      const size = { target_notional: nom, max_slippage_bps: 150 };
      const risk = await marketProvider.preOrder({ ctx, signal, size });
      setLastRisk(risk);
      if (!risk.allow) {
        setLastReject({ tags: risk.tags, notes: risk.notes ?? "" });
        return;
      }
      const intent = {
        side: nextSide,
        order_type: "market" as const,
        qty_or_notional: risk.clipped_size ?? nom,
        max_slippage_bps: 150,
        client_tag: "paper-trade-ui",
      };
      const out = await marketProvider.paperOrder({ ctx, intent, risk });
      if (out.reject) {
        setLastReject(out.reject);
        setLastFills([]);
        return;
      }
      setLastFills(
        (out.fills ?? []).map((f) => ({
          ...f,
          symbol,
        }))
      );
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function submitPipeline() {
    setBusy(true);
    setError("");
    setLastReject(null);
    setLastFills([]);
    setLastPath("pipeline");
    try {
      if (!(nom > 0)) throw new Error("notional must be > 0");
      const out = await marketProvider.decideAndFill({
        symbol,
        side,
        notional: nom,
        strategyId: "pipeline-v0",
        spread_bps: wideSpread ? 200 : undefined,
      });
      setLastRisk(out.risk);
      if (out.reject) {
        setLastReject(out.reject);
        setLastFills([]);
        return;
      }
      setLastFills((out.fills ?? []).map((f) => ({ ...f, symbol })));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="shell-page trade-page">
      <h1>交易 / Trade</h1>
      <p className="muted">
        仅纸面 PaperBroker · venue=<code>pump.fun</code>。提交走 <code>pre-order</code> →{" "}
        <code>paper/orders</code>；拒单不画 Fill。无私钥、无 sniper。行情叠加请切{" "}
        <code>paper</code> / <code>pumpfun_paper</code>。
      </p>

      <div className="trade-toolbar">
        <div className="ws-status" data-status={wsStatus}>
          WS {wsStatus}
          {mid != null ? ` · mid ${fmt(mid)}` : ""}
        </div>
        <div className="data-source-toggle" role="group" aria-label="dataSource">
          {DATA_SOURCES.map((opt) => (
            <button
              key={opt}
              type="button"
              className={dataSource === opt ? "active" : ""}
              onClick={() => setDataSource(opt)}
            >
              {opt}
            </button>
          ))}
        </div>
        <Link className="muted" to="/settings">
          Settings
        </Link>
      </div>

      <section className="paper-order-panel" aria-label="paper order">
        <h2>Paper order</h2>
        <div className="paper-form">
          <label>
            Symbol
            <select value={symbol} onChange={(e) => setSymbol(e.target.value)}>
              {(symbols.length ? symbols : [{ symbol: DEFAULT_SYMBOL, base: "PEPE", quote: "SOL" } as SymbolInfo]).map(
                (s) => (
                  <option key={s.symbol} value={s.symbol}>
                    {s.base}/{s.quote} · {truncateMint(s.mint ?? s.symbol)}
                  </option>
                )
              )}
            </select>
          </label>
          <label>
            Notional (SOL)
            <input
              type="number"
              min={0.001}
              step={0.01}
              value={notional}
              onChange={(e) => setNotional(e.target.value)}
            />
          </label>
          <label className="checkbox">
            <input
              type="checkbox"
              checked={wideSpread}
              onChange={(e) => setWideSpread(e.target.checked)}
            />
            Wide spread (deny)
          </label>
        </div>
        <div className="paper-actions">
          <button
            type="button"
            className="buy"
            disabled={busy}
            onClick={() => submitTwoStep("buy")}
          >
            Buy
          </button>
          <button
            type="button"
            className="sell"
            disabled={busy}
            onClick={() => submitTwoStep("sell")}
          >
            Sell
          </button>
          <button type="button" className="ghost" disabled={busy} onClick={submitPipeline}>
            One-shot pipeline
          </button>
        </div>
        <p className="muted tiny">
          Buy/Sell：先 <code>POST /api/v1/risk/pre-order</code>，allow 后再{" "}
          <code>POST /api/v1/paper/orders</code>。One-shot：
          <code>POST /api/v1/pipeline/decide-and-fill</code>（服务端用 mock mid/book 组 ctx）。拒单后该
          symbol 约 30s cooldown。
        </p>
        {busy ? <p className="muted">submitting…</p> : null}
        {error ? <p className="error">{error}</p> : null}
      </section>

      <div className="trade-columns">
        <section className="trade-result">
          <h2>Last HTTP result {lastPath ? `· ${lastPath}` : ""}</h2>
          {lastRisk ? (
            <p>
              RiskGate{" "}
              <strong className={lastRisk.allow ? "ok-text" : "error"}>{lastRisk.allow ? "ALLOW" : "DENY"}</strong>
              {lastRisk.clipped_size != null ? ` · clipped ${fmt(lastRisk.clipped_size, 5)}` : ""}
              {lastRisk.tags?.length ? ` · ${lastRisk.tags.join(", ")}` : ""}
              {lastRisk.notes ? ` · ${lastRisk.notes}` : ""}
            </p>
          ) : (
            <p className="muted">尚未下单。</p>
          )}
          {lastReject ? (
            <div className="reject-box">
              <strong>Reject</strong>
              <div>{lastReject.tags.join(", ") || "—"}</div>
              <div className="muted">{lastReject.notes}</div>
            </div>
          ) : null}
          {lastFills.length ? (
            <table className="fill-table">
              <thead>
                <tr>
                  <th>ts</th>
                  <th>price</th>
                  <th>qty</th>
                  <th>fee</th>
                  <th>slip bps</th>
                  <th>tag</th>
                </tr>
              </thead>
              <tbody>
                {lastFills.map((f, i) => (
                  <tr key={`${f.ts}-${i}`} className={f.qty >= 0 ? "buy" : "sell"}>
                    <td>{f.ts}</td>
                    <td>{fmt(f.price)}</td>
                    <td>{fmt(f.qty)}</td>
                    <td>{fmt(f.fee ?? 0)}</td>
                    <td>{fmt(f.slippage_bps ?? 0, 4)}</td>
                    <td>{f.tag ?? ""}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : lastPath && !lastReject ? (
            <p className="muted">no fills</p>
          ) : null}
        </section>

        <section className="trade-tape">
          <h2>WS tape</h2>
          <p className="muted tiny">hub: signal · risk · fill · reject · trading_state</p>
          <ul>
            {tape.length === 0 ? <li className="muted">等待事件…</li> : null}
            {tape.map((row) => (
              <li key={row.id} data-kind={row.kind}>
                <span className="kind">{row.kind}</span>
                <span>{row.text}</span>
              </li>
            ))}
          </ul>
        </section>
      </div>
    </div>
  );
}
