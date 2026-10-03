# 纸面 vs 回测预期区间 + 账本版本记录（P0-3 / P0-4）

## 预期区间
- 生成：`python -m app.backtest.expected_band --archive <binance-archive>`，输出 `app/backtest/bands/trend_tsmom_v1.json`（已提交）。
- 方法：对回测 hold-out 的日收益（2025-01-09..2026-09-30，共 630 天，风控关闭，和验收一致）做 moving-block bootstrap。得到运行 N 天（N=1..365）后累计收益的 5% / 50% / 95% 分位，以及 N 天内最大回撤的 5% / 50% 分位。
- **事先登记的参数**：seed 20261003、block 20、2000 条路径、期限 365 天，分位取 5/50/95。文件里同时写了来源收益的 SHA-256。改参数就必须出新版本文件，不允许原地修改。
- 判定（`app/paper/expected_band.py`）：
  - 累计收益低于 p05 记为 `below`，高于 p95 记为 `above`；
  - 累计收益在区间内、但回撤比 5% 最差情形还深，记为 `dd_breach`；
  - 其余为 `inside`。
- 告警（`app/alerts.py` 的 `_band`）：进入新的“区间外”状态时发一封邮件；一直留在区间外的话每 7 天再提醒一次；回到区间内就重置。每日摘要里附带区间状态。
- **这不是 Go 判定**，只用于提前发现“跟回测不是一回事”。Go/No-Go 仍然要 ≥250 天。另外纸面开着风控，回测没开，偏差应在这个前提下解读。

## 账本版本
- `runs` 新增三列：`git_commit`、`params_sha`、`cost_model_sha`。用 ALTER 加列，老数据保持 NULL。
  - `params_sha` 是以下内容的 SHA-256：策略 describe、band、min_trade、start_nav、风控限额。
  - `cost_model_sha` 是 CostModel 字段的 SHA-256。
- 只是元数据，不参与任何计算。控制台显示“本次运行版本”；当前参数和上次运行不一致时会提示。
