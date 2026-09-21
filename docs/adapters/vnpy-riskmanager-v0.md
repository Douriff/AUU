# vn.py RiskManager → RiskGate（MIT 思路 · 不嵌 vn.py）

对照 vn.py `RiskManager` 硬闸清单，映射到现有 AUU `RiskGate.check` / `post_fill`。**本轮不加新钩子**；成交仍只走 `PaperBroker`。

| vn.py 概念 | AUU 已有 | 行为 |
|------------|----------|------|
| 交易开关 / engine status | `trading_state` halted | `TRADING_HALTED` 拒新开 |
| 只减仓 | `reducing` | `REDUCE_ONLY` |
| 单笔数量 / 名义 | `max_notional_per_symbol` clip | `POSITION_CAP` |
| 非法 / 零名义 | `PaperBroker.submit` | `MAX_NOTIONAL` |
| 日亏熔断 | `max_day_loss_pct` + `post_fill` | `DAY_LOSS_BREAKER` → halted |
| 流控 / 拒单冷却 | `cooldown_sec_after_reject` | `COOLDOWN` |
| 价差 / 冲击 | `max_spread_bps` / curve impact | `SPREAD_TOO_WIDE` / `SLIPPAGE_CAP` |

成交路径：`RiskGate.check` → `PaperBroker.submit` → 可选 `post_fill`。无第二套下单器。

禁止：嵌 GPL/AGPL、真仓、sniper、QC 密钥、vectorbt。
