# 纸面平仓推演 + ExecReport · 接口草图 v0.1

对齐：望舒 P0=C。约束：`liveEnabled=false`；不改主 Go 窗参数。
主窗冻结（第八轮B）：progress **1500–6000** / TP0.06 / hold120 / SL0.05 / SP1.0×5s / 动能 10·2.5。

数据源：`PaperTradeJournal` + `DecisionLog` + executability 投影（只读，无 LLM）。

## HTTP

| Method | Path |
|--------|------|
| GET | `/api/v1/strategy/pump-paper-v1/postmortem` |
| GET | `/api/v1/stats/postmortem` |
| GET | `/api/v1/stats/exec-report` |

Query：`window` / `n` / `from` / `to` / `rolling` / `scenario`。触 `progress_*` → `400 SCENARIO_PROGRESS_FORBIDDEN`。

`PostmortemReport` 内嵌 `exec`（ExecReport）与 `scenario`（默认 `tag=off`）。`momentum_delta` 只允许 `min_trade_count_1m` / `min_buy_sell_ratio_1m`。`live_hint` 恒 `null`。`overall_go=false` 时附加 finding `GO_BLOCKED`。Setup `setup_seed_tags` 只进 notes/debug，不写 `HabitProfile`。

Findings：`SAMPLE_THIN`、`MH_DOMINANT`、`TP_HEALTHY`、`SL_HEAVY`、`IMPACT_Q4_DRAG`、`E_NEG`、`E_WEAK`、`SP_SILENT`、`GO_BLOCKED`。中文模板填数，无实盘/跟单建议。
