# 影子成交（paper-only）v0

对齐望舒 brief + `docs/adapters/decision-log-v0.md`。只加 DecisionLog 字段，不改冻结 Signal/Risk/Fill 事件名。  
**不**嵌 LGPL/GPL runtime；**不**发链；`liveEnabled` 仍 false。

---

## 字段

| 字段 | 含义 |
|------|------|
| `estimated_impact_bps` | 与 `impact_bps_est` 同值（曲线试算冲击） |
| `fill_px` | 纸面成交价 |
| `shadow_fill_px` | replay 对照价 |
| `shadow_slippage_bps` | `|shadow_fill_px − fill_px| / fill_px × 1e4` |
| `impact_error_bps` | `shadow_slippage_bps − estimated_impact_bps` |
| `shadow_source` | `next_trade` \| `next_open` \| `fill_quote` |

无成交可空。IS（implementation shortfall）本 v0 不做。

---

## P0 replay

1. **next_trade**：同一 `symbol` 曲线 tape 上 `ts > fill_ts` 的第一笔成交价（优先）
2. **next_open**：否则下一根 1m K 开盘
3. **fill_quote**：仍无对照时退回成交当时 `tick.mid`

写入：`paper_submit` 成交行；`GET /api/v1/stats/executability` 聚合前再 backfill 一次（tape 已前进时补上 next_trade）。

---

## 聚合 / Go-NoGo

`GET /api/v1/stats/executability` 增加 `impact_error.{p50_bps,p90_bps,n}`。  
门不变：`n_closed≥30` 且 `sample_ok`；`expectancy≥0`；入场冲击中位 &lt;60（硬顶 80）；影子滑点 P50 ≤40。  
`liveEnabled` 恒 false。

版本：v0。
