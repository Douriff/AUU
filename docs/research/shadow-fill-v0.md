# 影子成交（paper-only）v0

对齐望舒锁定 brief：`docs/research/auu-shadow-fill-channel-brief.md`、`docs/research/auu-shadow-fill-impact-refs.md`。  
只加 DecisionLog 字段，不改冻结 Signal/Risk/Fill 事件名。  
**不**嵌 LGPL/GPL runtime（Nautilus / backtrader / Freqtrade / blotter R 源码）；**不**发链；`liveEnabled` 仍 false。  
IS（Complete/Wagner）= P1，本 v0 不做。

---

## 字段（P0）

| 字段 | 含义 |
|------|------|
| `decision_px` / `arrival_px` | 决策/到达价（信号刻 curve mid） |
| `estimated_impact_bps` | 与 `impact_bps_est` 同值（下单前曲线/公式冲击） |
| `paper_fill_px` / `fill_px` | PaperBroker 记账价 |
| `shadow_fill_px` | 下一笔 tape 或下一根 1m open |
| `shadow_slippage_bps` | `sign(side) * (shadow_fill_px − decision_px) / decision_px × 1e4`（卖反向） |
| `impact_error_bps` | `shadow_slippage_bps − estimated_impact_bps`（正=预估偏乐观） |
| `shadow_source` | `next_trade` \| `next_open` \| `fill_quote` |

无成交可空。

---

## P0 replay

1. **next_trade**：同一 `symbol` 曲线 tape 上 `ts > t0` 的第一笔成交价（优先）
2. **next_open**：否则下一根 1m K 开盘
3. **fill_quote**：仍无对照时退回决策时刻 `tick.mid`（弱证据）

写入：`paper_submit` 成交行；`GET /api/v1/stats/executability` 聚合前再 backfill 一次（tape 已前进时补上 next_trade）。

---

## 聚合 / Go-NoGo

`GET /api/v1/stats/executability`：`median_entry_impact_bps` + `shadow_slippage.{p50_bps,p90_bps}` + `impact_error.{p50_bps,p90_bps,n}`。  
**门不变**：`n_closed≥30` 且 `sample_ok`；`expectancy≥0`；入场冲击中位 &lt;60（硬顶 80）；影子滑点 P50 ≤40。  
`liveEnabled` 恒 false。

版本：v0。
