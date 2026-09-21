# Jesse 风格蒙特卡洛（AUU 自研 · MIT 思路）

**不**嵌入 Jesse runtime、不拷 GPL。只借 `monte_carlo_trades` 节奏。

## P1（本轮）`monte_carlo_trades`

对 `PaperTradeJournal` 已实现的 round-trip `pnl_pct`：

1. **resample**：有放回抽 n 笔，连乘重建权益
2. **reshuffle**：打乱顺序（不放回），连乘重建权益

默认 `method=resample`，`n_paths=1000`，`seed=42`。仅 `GET .../stats?mc=1`。

输出（仅当 `sample_ok`，n≥10）：终值百分点 p5/p50/p95、`p_equity_positive`、`p_equity_above_start`、`p_hit_day_loss`。  
n&lt;10：`sample_ok=false`，`note=样本不足`，**无**百分位字段。

这是纸面历史模拟，不是收益承诺。

## P2（跳过）`monte_carlo_candles`

按 K 线路径重放策略。本轮不做。
