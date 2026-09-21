import { useStrategyConfig } from "@/hooks/useStrategyConfig";
import { PaperStatsPanel } from "@/components/market/PaperStatsPanel";
import { AutoPaperToggle } from "@/components/layout/AutoPaperToggle";
import type { PumpPaperParams } from "@/types/contracts";

const FIELDS: { key: keyof PumpPaperParams; label: string; step?: string }[] = [
  { key: "progress_bps_min", label: "progress_bps_min" },
  { key: "progress_bps_max", label: "progress_bps_max" },
  { key: "max_impact_bps", label: "max_impact_bps", step: "1" },
  { key: "take_profit_pct", label: "take_profit_pct", step: "0.01" },
  { key: "stop_loss_pct", label: "stop_loss_pct", step: "0.01" },
  { key: "max_hold_sec", label: "max_hold_sec" },
  { key: "cooldown_sec", label: "cooldown_sec" },
  { key: "max_day_loss_pct", label: "max_day_loss_pct", step: "0.01" },
  { key: "max_open_mints", label: "max_open_mints" },
  { key: "notional_pct_equity", label: "notional_pct_equity", step: "0.001" },
  { key: "max_notional_sol", label: "max_notional_sol", step: "0.01" },
];

export function StrategyPage() {
  const { params, autoPaperOrders, tradingState, patch, setAutoPaperOrders, err } = useStrategyConfig();

  return (
    <div className="shell-page">
      <h1>策略 / Strategy</h1>
        <p className="muted">
          <code>pump-paper-v1</code> 纸面策略。默认 <code>progress_bps [1200,6500]</code>、
          <code>max_impact_bps 75</code>（含费硬顶 80）、名义 0.5% 权益且单笔 ≤ 0.12 SOL；<code>auto_paper_orders</code> /{" "}
          <code>strategy_autopaper</code> 默认关。行情/交易页可看纸面成功概率（平仓后）。
          发现（<code>new_token</code>）只入自选，仍过 progress / 动能 / 冲击门。无钱包、无 sniper。
        </p>
      <p className="muted">
        trading_state=<code>{tradingState}</code>
      </p>
      <section className="settings-section">
        <h2>auto_paper_orders</h2>
        <AutoPaperToggle
          checked={autoPaperOrders}
          onChange={(v) => void setAutoPaperOrders(v).catch(() => undefined)}
          label="自动纸面下单（默认关）"
        />
      </section>
      <PaperStatsPanel />
      {params ? (
        <form
          className="strategy-form"
          onSubmit={(e) => {
            e.preventDefault();
            const fd = new FormData(e.currentTarget);
            const next: Partial<PumpPaperParams> = {};
            for (const { key } of FIELDS) {
              const raw = fd.get(key);
              if (typeof raw === "string" && raw !== "") {
                (next as Record<string, number>)[key] = Number(raw);
              }
            }
            void patch(next);
          }}
        >
          <dl className="settings-dl">
            {FIELDS.map(({ key, label, step }) => (
              <span key={key} className="strategy-field">
                <dt>
                  <label htmlFor={`sp-${key}`}>{label}</label>
                </dt>
                <dd>
                  <input
                    id={`sp-${key}`}
                    name={key}
                    type="number"
                    step={step ?? "1"}
                    defaultValue={params[key] as number}
                  />
                </dd>
              </span>
            ))}
          </dl>
          <button type="submit" className="primary-btn">
            保存参数
          </button>
        </form>
      ) : (
        <p className="muted">加载策略参数…</p>
      )}
      {err ? <p className="error">{err}</p> : null}
    </div>
  );
}
