# 纸面表现统计 v1

对齐锁定合同：`docs/adapters/paper-trade-journal-stats-v0.md`、`docs/viz/success-prob-panel-v0.md`、`docs/strategies/jesse-montecarlo-for-auu.md`。

P0 开源调研：

- Journal：手动 Trade + autopaper Fill → FIFO `RoundTrip`
- 胜率 / 期望 / 回撤 **自 journal 成交计算**（`win_rate = wins / n_trades`，pnl>0；QuantStats 仅指标思路，不进依赖）
- `GET /api/v1/strategy/pump-paper-v1/stats`；`POST .../stats/reset`
- trades-MC 默认关；`?mc=1` 才跑 homemade shuffle/bootstrap（Jesse `monte_carlo_trades` 思路，MIT；**不嵌 Jesse runtime**；`monte_carlo_candles` = P2 skip）
- `sample_ok = n_trades >= 20`；不足则面板「样本不足」，不画假分位带
- 权益 = `equity_0`（默认 10000）+ 累计已实现 pnl
- RiskGate 对照 vn.py RiskManager：`docs/adapters/vnpy-riskmanager-v0.md`；**无新风控钩子**；成交只 `PaperBroker`
- 不嵌 GPL/AGPL、vectorbt、QC 密钥；无真仓 / sniper
- 观察对照：胜率旁「对照」折叠，见 `docs/viz/trader-watch-ui-v0.md` §5；`trader_ref` **不**并入胜率分母
- 可执行性：PaperStats 下「可执行性证据」；`GET /api/v1/stats/executability` 聚合 Journal + **DecisionLog**；桶为 `progress|impact|risk`；影子 replay 见 `docs/research/shadow-fill-v0.md`；`liveEnabled` 仍关。见 `docs/viz/executability-panel-v0.md`、`docs/adapters/decision-log-v0.md`
