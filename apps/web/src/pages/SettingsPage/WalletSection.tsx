import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { useConnection, useWallet } from "@solana/wallet-adapter-react";
import { useWalletModal } from "@solana/wallet-adapter-react-ui";
import { Transaction } from "@solana/web3.js";
import { Buffer } from "buffer";
import { marketProvider } from "@/providers/HttpWsProvider";
import type { AuthMe, WalletLedger, WalletStatus } from "@/types/contracts";

function bytesToBase64(bytes: Uint8Array): string {
  let text = "";
  for (const byte of bytes) text += String.fromCharCode(byte);
  return btoa(text);
}

function dayPercent(fraction: number): string {
  return String(Math.round(fraction * 1000) / 10);
}

export function WalletSection() {
  const { connection } = useConnection();
  const { publicKey, connected, signMessage, sendTransaction, disconnect } = useWallet();
  const { setVisible } = useWalletModal();
  const [me, setMe] = useState<AuthMe | null>(null);
  const [status, setStatus] = useState<WalletStatus | null>(null);
  const [ledger, setLedger] = useState<WalletLedger | null>(null);
  const [sol, setSol] = useState("1");
  const [day, setDay] = useState("4.5");
  const [slots, setSlots] = useState("10");
  const [seeded, setSeeded] = useState(false);
  const [accept, setAccept] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [note, setNote] = useState("");

  async function refresh(auth: AuthMe | null) {
    const next = await marketProvider.getWalletStatus();
    setStatus(next);
    if (auth?.auth_enabled && auth.user) {
      setLedger(await marketProvider.getWalletLedger());
    } else {
      setLedger(null);
    }
    return next;
  }

  useEffect(() => {
    let gone = false;
    marketProvider
      .getMe()
      .catch(() => null)
      .then(async (auth) => {
        if (gone) return;
        setMe(auth);
        try {
          await refresh(auth);
        } catch (e) {
          if (!gone) setError(e instanceof Error ? e.message : "钱包状态读取失败");
        }
      });
    return () => {
      gone = true;
    };
  }, []);

  useEffect(() => {
    if (!status || seeded) return;
    setSol(String(status.risk.max_notional_sol));
    setDay(dayPercent(status.risk.max_day_loss_pct));
    setSlots(String(status.risk.max_open_positions));
    setSeeded(true);
  }, [status, seeded]);

  const connectedKey = publicKey?.toBase58() ?? "";
  const loggedIn = Boolean(me?.auth_enabled && me.user);
  const mismatch = Boolean(status?.pubkey && connectedKey && connectedKey !== status.pubkey);

  async function run(action: () => Promise<void>) {
    setBusy(true);
    setError("");
    setNote("");
    try {
      await action();
    } catch (e) {
      setError(e instanceof Error ? e.message : "操作失败");
    } finally {
      setBusy(false);
    }
  }

  async function bind() {
    if (!publicKey || !signMessage) {
      setError("请先连接支持消息签名的钱包");
      return;
    }
    await run(async () => {
      const pubkey = publicKey.toBase58();
      const challenge = await marketProvider.walletChallenge(pubkey);
      const signature = await signMessage(new TextEncoder().encode(challenge.message));
      const next = await marketProvider.walletBind({
        pubkey,
        nonce: challenge.nonce,
        signature: bytesToBase64(signature),
      });
      setStatus(next);
      setNote("公钥已绑定。此签名不授权转账或下单。");
    });
  }

  async function unbind() {
    await run(async () => {
      setStatus(await marketProvider.walletUnbind());
      setNote("公钥已解绑。历史流水仍保留。");
    });
  }

  async function saveRisk() {
    const notional = Number(sol);
    const dayLoss = Number(day) / 100;
    const positions = Number(slots);
    if (!(notional > 0 && notional <= 1)) {
      setError("单笔上限须大于 0 且不超过 1 SOL");
      return;
    }
    if (!(dayLoss > 0 && dayLoss <= 0.045 + 1e-9)) {
      setError("日亏熔断须大于 0 且不超过 4.5%");
      return;
    }
    if (!Number.isInteger(positions) || positions < 1 || positions > 10) {
      setError("同时持仓须为 1 到 10 的整数");
      return;
    }
    await run(async () => {
      const next = await marketProvider.putWalletRisk({
        max_notional_sol: notional,
        max_day_loss_pct: dayLoss,
        max_open_positions: positions,
      });
      setStatus(next);
      setNote("风控已保存。只能比平台硬上限更严。");
    });
  }

  async function setMode(enabled: boolean) {
    if (enabled && !accept) {
      setError("开启前请勾选风险提示");
      return;
    }
    await run(async () => {
      const next = await marketProvider.setWalletMode({ enabled, accept_risk: accept });
      setStatus(next);
      setNote(enabled ? "钱包模式已开启。链上交易仍要你本人确认。" : "钱包模式已关闭。");
    });
  }

  async function sendMemo() {
    if (!publicKey || !sendTransaction) {
      setError("请先连接钱包");
      return;
    }
    if (status?.pubkey && publicKey.toBase58() !== status.pubkey) {
      setError("请连接已绑定的公钥");
      return;
    }
    await run(async () => {
      const prepared = await marketProvider.prepareWalletMemo();
      const tx = Transaction.from(Buffer.from(prepared.tx_base64, "base64"));
      const signature = await sendTransaction(tx, connection);
      const recorded = await marketProvider.recordWalletTx({
        prepare_id: prepared.prepare_id,
        signature,
      });
      setNote(`已提交 devnet memo，状态 ${recorded.item.status}`);
      await refresh(me);
    });
  }

  async function halt(on: boolean) {
    await run(async () => {
      await marketProvider.haltWallets(on);
      await refresh(me);
      setNote(on ? "已关闭所有人的钱包模式。" : "总开关已解除。每个人仍需重新开启。");
    });
  }

  const modeLabel = status?.wallet_mode === "devnet" ? "devnet" : status?.wallet_mode === "mainnet" ? "mainnet（本阶段不可下单）" : "关闭";

  return (
    <section className="settings-section wallet-section">
      <h2>非托管钱包</h2>
      <p className="wallet-banner">Devnet 测试，不是真钱。平台只保存公钥，不保存私钥，不托管资金，不代签名。</p>
      <p className="muted">
        支持 Phantom、Solflare。总开关当前为 <strong>{modeLabel}</strong>
        {status?.global_halt ? "，管理员已一键关闭所有人的钱包模式。" : "。"}
        新用户默认纸面。开启钱包模式前需要勾选风险提示。
      </p>
      {status ? <p className="muted wallet-copy">{status.risk_text}</p> : null}
      {error ? <p className="warn">{error}</p> : null}
      {note ? <p className="muted">{note}</p> : null}

      {!me?.auth_enabled ? (
        <p className="muted">绑定公钥需要开启账户（AUU_AUTH=on）并登录。本地单用户模式仍只做纸面。</p>
      ) : null}
      {me?.auth_enabled && !me.user ? (
        <p className="muted">
          <Link to="/login">登录</Link> 后可以绑定公钥。
        </p>
      ) : null}

      <div className="wallet-actions">
        {connected ? (
          <button type="button" onClick={() => void disconnect()}>
            断开钱包
          </button>
        ) : (
          <button type="button" onClick={() => setVisible(true)}>
            连接钱包
          </button>
        )}
        {loggedIn ? (
          <button type="button" disabled={busy || !connected} onClick={() => void bind()}>
            绑定公钥
          </button>
        ) : null}
        {loggedIn && status?.bound ? (
          <button type="button" disabled={busy} onClick={() => void unbind()}>
            解绑公钥
          </button>
        ) : null}
      </div>
      <p className="muted">
        浏览器钱包 {connectedKey || "未连接"}
        {status?.pubkey ? ` · 已绑定 ${status.pubkey}` : " · 尚未绑定"}
        {mismatch ? " · 当前连接的公钥和已绑定公钥不一致" : ""}
      </p>
      {status?.consent ? (
        <p className="muted">
          已同意 {status.consent.version} · {new Date(status.consent.ts).toLocaleString()}
        </p>
      ) : null}

      {loggedIn ? (
        <form
          className="auth-card wallet-grid"
          onSubmit={(event) => {
            event.preventDefault();
            void saveRisk();
          }}
        >
          <label>
            单笔上限 SOL
            <input value={sol} onChange={(e) => setSol(e.target.value)} inputMode="decimal" />
          </label>
          <label>
            日亏熔断 %
            <input value={day} onChange={(e) => setDay(e.target.value)} inputMode="decimal" />
          </label>
          <label>
            最大同时持仓
            <input value={slots} onChange={(e) => setSlots(e.target.value)} inputMode="numeric" />
          </label>
          <button type="submit" disabled={busy}>
            保存风控
          </button>
        </form>
      ) : null}
      <p className="muted">平台硬上限 1 SOL / 4.5% / 10 仓。这里只能调得更严。</p>

      {loggedIn ? (
        <label className="wallet-check">
          <input type="checkbox" checked={accept} onChange={(e) => setAccept(e.target.checked)} />
          我已阅读风险提示：每一笔链上交易都要我本人在钱包里确认，交易不可撤销，网络费从我的钱包支付。
        </label>
      ) : null}
      {loggedIn ? (
        <div className="wallet-actions">
          <button type="button" disabled={busy || status?.wallet_enabled === true} onClick={() => void setMode(true)}>
            开启钱包模式
          </button>
          <button type="button" disabled={busy || !status?.wallet_enabled} onClick={() => void setMode(false)}>
            关闭钱包模式
          </button>
          <button type="button" disabled={busy || !status?.tx_allowed || !connected || mismatch} onClick={() => void sendMemo()}>
            签名并发送 devnet memo
          </button>
        </div>
      ) : null}
      <p className="muted">示例是金额为 0 的 devnet memo，不是转账。请在钱包里把网络切到 Devnet。未签名交易由服务端组好，签名和广播都在你的钱包里完成。</p>

      {me?.user?.is_admin ? (
        <div className="wallet-actions">
          <button type="button" disabled={busy || status?.global_halt === true} onClick={() => void halt(true)}>
            关闭所有人的钱包模式
          </button>
          <button type="button" disabled={busy || !status?.global_halt} onClick={() => void halt(false)}>
            解除总开关
          </button>
        </div>
      ) : null}

      {loggedIn ? (
        <>
          <h3>钱包流水</h3>
          <table className="wallet-table">
            <thead>
              <tr>
                <th>时间</th>
                <th>网络</th>
                <th>公钥</th>
                <th>签名</th>
                <th>状态</th>
                <th>金额 SOL</th>
              </tr>
            </thead>
            <tbody>
              {(ledger?.items ?? []).length === 0 ? (
                <tr>
                  <td colSpan={6}>还没有钱包流水</td>
                </tr>
              ) : (
                ledger?.items.map((item) => (
                  <tr key={item.id}>
                    <td>{item.ts ? new Date(item.ts).toLocaleString() : ""}</td>
                    <td>{item.network}</td>
                    <td className="sig" title={item.pubkey}>{item.pubkey}</td>
                    <td className="sig" title={item.signature}>{item.signature}</td>
                    <td>{item.status}</td>
                    <td>{item.amount_sol}</td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </>
      ) : null}
    </section>
  );
}
