# 决策日志 · 拒单原因码 v0（可执行性证据）

对齐望舒门槛：平仓≥30 + sample_ok；期望≥0；入场冲击**扣费**中位<60bps（含费硬顶80）；影子滑点；拒单可解释。  
原则：只加日志字段，不改冻结 Signal/Risk/Fill 事件名。

---

## 1. 稳定原因码（`reason` / `tags`）

策略 `SignalOut.reason` 与 RiskGate `RiskOut.tags` 必须用下列稳定串（可并存 tags）：

| 码 | 来源 | 含义 |
|----|------|------|
| `progress_band` | pump-paper-v1 | 未过 progress 入场窗 |
| `momentum` | pump-paper-v1 | tape 动能不足 |
| `impact` | pump-paper-v1 / 预估 | 试算冲击未过策略闸 |
| `SLIPPAGE_CAP` | RiskGate | 硬顶/冲击超限 |
| `DEPTH_THIN` / `SPREAD_TOO_WIDE` | RiskGate | 流动性 |
| `DAY_LOSS_BREAKER` / `TRADING_HALTED` / `COOLDOWN` / `POSITION_CAP` | RiskGate | 账户/状态 |
| `LIVE_DISABLED` | LiveLimits | 实盘未开闸 |
| `not_curve` / `blocked_tag` / `max_open_mints` | 策略 | 其它策略侧拒 |

聚合桶（可执行性面板）：
- `progress` ← `progress_band` / `not_curve`
- `impact` ← `impact` / `SLIPPAGE_CAP` / `DEPTH_THIN`
- `risk` ← 日亏/halt/cooldown/position/live

---

## 2. `DecisionLog` 行（每决策一条）

```text
DecisionLog:
  ts, strategy_id, symbol, mint?
  stage: signal | pre_order | paper_submit | live_blocked
  signal_side, signal_reason, signal_tags[]
  risk_allow, risk_tags[], risk_notes
  notional_sol?, impact_bps_est?, impact_bps_cap?
  impact_gross_bps?, protocol_fee_bps?, impact_net_bps?, phase?
  # gross = 现有 estimated impact（含费）；net = max(0, gross − protocol_fee_bps)
  # curve/graduating 费地板 62.5；amm 10。不是 swap 路径的 DEFAULT_PROTOCOL_FEE_BPS=100
  shadow_slippage_bps?     # 纸面 Fill.slippage vs 预估，无成交可空
  outcome: emit_signal | reject | fill | partial
  reject_bucket: progress | impact | risk | none
```

写入：策略 evaluate 后 + `run_pre_order` 后各一条（或合并一条含两阶段）。  
查询：`GET /api/v1/strategy/pump-paper-v1/decision-log?from=&to=` → 供可执行性面板聚合拒单结构。

---

## 3. 与 Journal / Go-NoGo

- Journal 仍只计已平仓纸面 round-trip  
- 可执行性：`n_closed` / `sample_ok` / expectancy 来自 stats；冲击中位与拒单直方来自 DecisionLog  
- 开 live 前：面板 Go-NoGo 读上述阈值，不替代 `liveEnabled` 二次确认
