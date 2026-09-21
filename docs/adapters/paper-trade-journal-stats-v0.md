# PaperTradeJournal + stats v0

状态：合同（2026-09-21）  
模式：**仅纸面**。胜率必须从 round-trip **trades 自算**；QuantStats 只借指标思路，不进主仓。vectorbt / Jesse runtime / QuantConnect 密钥禁止。

## Journal

`PaperTradeJournal` 消费同一条 `run_pre_order` → `run_paper_order` Fill：

- 手动 Trade / `decide-and-fill`
- `strategy_autopaper` 自动单

FIFO 配对成 `RoundTrip`：`entry_*` / `exit_*` / `pnl` / `pnl_pct` / `tags` / `source=manual|autopaper`。未平仓 lot 不计胜率。

Autopaper：**off** 只发 `signal`；**on** 且 `trading_state=active` 才下纸面单。不新增 RiskGate 钩子。

## REST

`GET /api/v1/strategy/pump-paper-v1/stats`

- 默认 **无** 蒙特卡洛（`mc` 缺省 / `mc=0`）
- `?mc=1` 才跑 trades-MC（见 `docs/strategies/jesse-montecarlo-for-auu.md`）
- 别名：`GET /api/v1/stats/paper-performance`

包络 `{ ok, data }`。`win_rate = wins / trade_count`（`pnl>0`）。`sample_ok` 当 `trade_count >= 10`。`sample_ok=false` 时 **不** 返回百分位带。

字段：`trade_count, wins, losses, win_rate, expectancy_pnl_pct, expectancy_r, max_drawdown_pct, equity[], journal[]`。
