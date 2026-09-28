import { useEffect, useState } from "react";
import { useWallet } from "@solana/wallet-adapter-react";
import { useWalletModal } from "@solana/wallet-adapter-react-ui";
import { Transaction, VersionedTransaction } from "@solana/web3.js";
import { Buffer } from "buffer";
import { marketProvider } from "@/providers/HttpWsProvider";
import type { WalletSignal } from "@/types/contracts";

function price(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n)) return "—";
  if (n >= 1) return n.toFixed(4);
  return n.toFixed(8);
}

function reasonLabel(reason: string): string {
  if (reason === "pump_paper_v1_entry") return "策略入场";
  if (reason === "take_profit") return "止盈";
  if (reason === "stop_loss") return "止损";
  if (reason === "hold") return "持有";
  if (reason === "levels") return "参考价位";
  return reason;
}

export function WalletSignalCard({
  mint,
  priceSol,
  sellPct = 100,
  symbol,
  compact = false,
}: {
  mint?: string;
  priceSol?: number | null;
  sellPct?: number;
  symbol?: string;
  compact?: boolean;
}) {
  const { signTransaction, connected } = useWallet();
  const { setVisible } = useWalletModal();
  const [signal, setSignal] = useState<WalletSignal | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [note, setNote] = useState("");

  useEffect(() => {
    let gone = false;
    marketProvider
      .getWalletSignal({ mint, price_sol: priceSol })
      .then((next) => {
        if (!gone) setSignal(next);
      })
      .catch((e: Error) => {
        if (!gone) setError(e.message);
      });
    return () => {
      gone = true;
    };
  }, [mint, priceSol]);

  async function order(side: "buy" | "sell") {
    if (!signal || !mint || priceSol == null) return;
    if (!connected || !signTransaction) {
      setVisible(true);
      setError("请先连接 Phantom 或 Solflare，并在钱包里切到主网");
      return;
    }
    setBusy(true);
    setError("");
    setNote("");
    try {
      const prepared = await marketProvider.prepareWalletOrder({
        mint,
        side,
        notional_sol: side === "buy" ? signal.suggested_sol : undefined,
        sell_pct: side === "sell" ? sellPct : undefined,
        price_sol: priceSol,
        slippage_bps: 150,
      });
      const raw = Buffer.from(prepared.tx_base64, "base64");
      let tx: VersionedTransaction | Transaction;
      try {
        tx = VersionedTransaction.deserialize(raw);
      } catch {
        tx = Transaction.from(raw);
      }
      const signed = await signTransaction(tx as never);
      const bytes = Buffer.from((signed as { serialize: () => Uint8Array }).serialize());
      const saved = await marketProvider.submitWalletOrder({
        prepare_id: prepared.prepare_id,
        signed_tx: bytes.toString("base64"),
      });
      setNote(`已提交真钱${side === "buy" ? "买入" : "卖出"}，状态 ${saved.item.status}。签名在你的钱包里完成。`);
      setSignal(await marketProvider.getWalletSignal({ mint, price_sol: priceSol }));
    } catch (e) {
      setError(e instanceof Error ? e.message : "下单失败");
    } finally {
      setBusy(false);
    }
  }

  const buyDisabled = busy || !signal?.order_allowed || signal.day_loss_tripped || !mint || priceSol == null;
  const sellDisabled = busy || !signal?.order_allowed || !signal.position || !mint;

  return (
    <section className={compact ? "wallet-signal is-compact" : "wallet-signal"} aria-label="策略信号">
      <p className={signal?.real_money && signal.order_allowed ? "wallet-real" : "wallet-banner"}>
        {signal?.real_money && signal.order_allowed
          ? "真钱。主网单要你本人在钱包里确认，不会自动下单，服务器不代签名。"
          : "真钱通道默认关闭。这里只展示策略参考，不会自动下单。"}
      </p>
      <h2>
        策略信号
        {symbol ? ` · ${symbol}` : ""}
        {signal ? ` · ${reasonLabel(signal.reason)}` : ""}
      </h2>
      <dl>
        <div>
          <dt>入场参考价</dt>
          <dd>{price(signal?.entry_price)}</dd>
        </div>
        <div>
          <dt>止盈 {signal ? `${(signal.take_profit_pct * 100).toFixed(1)}%` : ""}</dt>
          <dd>{price(signal?.take_profit)}</dd>
        </div>
        <div>
          <dt>止损 {signal ? `${(signal.stop_loss_pct * 100).toFixed(1)}%` : ""}</dt>
          <dd>{price(signal?.stop_loss)}</dd>
        </div>
        <div>
          <dt>建议仓位</dt>
          <dd>{signal ? `${signal.suggested_sol} SOL` : "—"}</dd>
        </div>
      </dl>
      <p className="muted">滑点上限 150 bps。建议仓位已按你的风控上限截断。{signal?.note}</p>
      {signal?.position ? (
        <p className="muted">
          钱包仓位 {signal.position.qty.toFixed(4)} · 成本 {price(signal.position.entry_price)} · 浮动{" "}
          {signal.position.upnl_sol.toFixed(4)} SOL
        </p>
      ) : null}
      {signal?.day_loss_tripped ? <p className="warn">日亏已到上限，买入已停止。卖出仍要你签名。</p> : null}
      {error ? <p className="warn">{error}</p> : null}
      {note ? <p className="muted">{note}</p> : null}
      {compact ? null : (
        <div className="wallet-actions">
          <button type="button" disabled={buyDisabled} onClick={() => void order("buy")}>
            真钱买入 {signal ? `${signal.suggested_sol} SOL` : ""}
          </button>
          <button type="button" disabled={sellDisabled} onClick={() => void order("sell")}>
            真钱卖出 {sellPct}%
          </button>
        </div>
      )}
    </section>
  );
}
