import { useEffect, useState } from "react";

/** Spec-locked ack. The only UI string allowed to mention 跟单/复制交易, as a denial. */
export const DISTILL_ACK = "我理解这是参数蒸馏，不是跟单/复制交易。";

export function DistillConfirmDialog({
  open,
  busy,
  onCancel,
  onConfirm,
}: {
  open: boolean;
  busy?: boolean;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  const [ack, setAck] = useState(false);

  useEffect(() => {
    if (open) setAck(false);
  }, [open]);

  if (!open) return null;

  return (
    <div className="modal-backdrop" role="presentation" onClick={onCancel}>
      <div
        className="modal-card"
        role="dialog"
        aria-labelledby="distill-confirm-title"
        onClick={(e) => e.stopPropagation()}
      >
        <h2 id="distill-confirm-title">应用蒸馏参数到自有策略？</h2>
        <ol className="tiny">
          <li>
            将把下列参数写入 <strong>自有</strong> <code>pump-paper-v1</code>
            ，不是自动复制该钱包的下一笔成交。
          </li>
          <li>
            <strong>不会</strong>打开实盘（<code>liveEnabled</code> 不变）；
            <strong>不会</strong>自动打开 <code>strategy_autopaper</code>
            （保持当前默认关，除非用户另开）。
          </li>
          <li>仍走 RiskGate → PaperBroker 纸面路径。</li>
        </ol>
        <label className="checkbox-row">
          <input type="checkbox" checked={ack} onChange={(e) => setAck(e.target.checked)} />
          {DISTILL_ACK}
        </label>
        <div className="paper-actions">
          <button type="button" className="primary-btn" disabled={!ack || busy} onClick={onConfirm}>
            确认应用
          </button>
          <button type="button" className="ghost" onClick={onCancel} disabled={busy}>
            取消
          </button>
        </div>
      </div>
    </div>
  );
}
