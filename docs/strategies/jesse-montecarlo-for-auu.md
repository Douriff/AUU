# Jesse Monte Carlo → AUU 成功概率对照（v0）

> 2026-09-21 · 源码：`jesse/research/monte_carlo/`（MIT）  
> 对齐：`PaperTradeJournal` + `GET .../stats` + 可选 MC（默认关）  
> 原则：只借思路；不嵌 Jesse 全局 runtime；不接真仓。

## 1. Jesse 两路 MC

| Jesse API | 机制 | 产出 | AUU 是否优先 |
|-----------|------|------|--------------|
| `jesse.research.monte_carlo_trades` | 对**已成交交易序列**做顺序重排 / 重采样，重建权益曲线 | 原序列 + N 情景：`total_return`/`max_drawdown`/`volatility`/`sharpe`… + `confidence_analysis`（分位、CI、p-value） | **P0**：直接吃 `PaperTradeJournal` round-trip |
| `jesse.research.monte_carlo_candles` | 对 K 线加噪 / MovingBlockBootstrap 后**重跑整段回测** | 每情景完整 metrics + equity + trades | P2：需完整 candle 回测引擎；本期可不做 |

辅助：`GaussianNoiseCandlesPipeline`、`MovingBlockBootstrapCandlesPipeline`（candle 路）；`CONFIDENCE_PERCENTILES`、5%/1% α（common.py）。

## 2. 接到 AUU

| AUU | 借 Jesse | 不借 |
|-----|----------|------|
| `PaperTradeJournal` 每笔 `{entry,exit,pnl,tags}` | 当作 `original_trades` 输入 | Jesse `Broker`/全局 store |
| `GET /api/v1/strategy/pump-paper-v1/stats` | 原序列胜率/期望/回撤 = journal 直接聚合（可不跑 MC） | — |
| 可选 MC（Settings 默认关） | **trades 重采样**思路：shuffle round-trip 顺序（或有放回 bootstrap pnl）→ 重建 equity → 汇总分位 | Ray 依赖、matplotlib 绑死、整仓 `monte_carlo_candles` |
| 可视化成功概率面板 | 摘要：胜率/期望/MaxDD/笔数；MC 开时加 p50/p5–p95 权益带 | Jesse 自带 plot_* |

**推荐实现草图（伪）：**
1. 无 MC：`stats = aggregate(journal)` → win_rate / expectancy / max_dd / n_trades / equity_curve  
2. 有 MC：`scenarios[i] = rebuild_equity(shuffle(journal.pnls))` × N（如 200–1000，seed 固定）→ 分位与「原曲线是否落在 5% 尾部」  
3. 模因币注意：短样本 n 小 → MC 置信带宽、面板标注「样本不足」；毕业/timeout 标签可分层重采样（可选）

## 3. 许可证

Jesse **MIT** → 可借鉴算法自研进 AUU；勿整文件 copyleft 混淆。勿引入生产密钥。
