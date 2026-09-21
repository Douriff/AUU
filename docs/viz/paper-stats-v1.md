# 纸面表现统计 v1（成功概率 / 蒙特卡洛）

状态：冻结草稿（2026-09-21）  
模式：**仅纸面 / 模拟**。由已平仓纸面成交重抽样，**不是**实盘胜率承诺，也不是未来收益保证。

对接：`PaperBroker` Fill → `PaperTradeJournal` → `GET /api/v1/strategy/pump-paper-v1/stats`（别名 `/api/v1/stats/paper-performance`）

合同：`docs/adapters/paper-trade-journal-stats-v0.md`、`docs/viz/success-prob-panel-v0.md`、`docs/strategies/jesse-montecarlo-for-auu.md`。

---

## 1. 样本

- **成交来源**：所有走 `RiskGate → PaperBroker` 的 Fill（手动 Trade、`decide-and-fill`、`strategy_autopaper` 自动单）。同一条 broker 路径，不另开撮合。
- **平仓笔**：FIFO `RoundTrip`。未平仓 lot 不计入胜率。`win_rate = wins / n`（trades 自算）。
- **窗口**：默认 `window=session`；`window=N` 取最近 N 笔。

空仓：`empty=true`，`win_rate=null`。`sample_ok` 要求 n≥10。前端无平仓时显示「暂无平仓样本」。

---

## 2. 公式

记第 \(i\) 笔已平仓交易收益率为 \(r_i = \text{pnl\_pct}\)（扣费后）：

| 多头 | \(r = (P_\text{exit}-P_\text{entry})/P_\text{entry} - \text{fee}/(P_\text{entry} Q)\) |
| 空头 | \(r = (P_\text{entry}-P_\text{exit})/P_\text{entry} - \text{fee}/(P_\text{entry} Q)\) |

| 字段 | 定义 |
|------|------|
| `trade_count` | 已平仓笔数 \(n\) |
| `wins` / `losses` | `pnl > 0` / `pnl < 0` |
| `win_rate` | **成功概率（胜率）** \(= wins / n\)（样本内，trades 自算） |
| `expectancy_pnl_pct` | \(\bar r \times 100\)（平均收益率，百分点） |
| `expectancy_r` | \(\bar r / \text{stop\_loss\_pct}\)（默认 SL=12%，平均 R） |
| `max_drawdown_pct` | 将 \(r_i\) 连乘权益曲线 \(E_0=1,\; E_{t}=E_{t-1}(1+r_t)\)，峰到谷 \((peak-E)/peak \times 100\) |
| `sharpe_like` | \(n \ge 5\) 时 \(\bar r / s_{r}\)（**交易收益样本**，不年化）；样本不足为 `null` |

---

## 3. 蒙特卡洛（默认关）

仅 `?mc=1`。对 `{r_i}` **resample**（或 `method=reshuffle`）重建权益。n&lt;10 时 `sample_ok=false`，返回 `note=样本不足`，**不**带 p5/p50/p95。

`monte_carlo.label` / 顶层 `disclaimer`：simulation from paper history, not a promise。

---

## 4. REST

`GET /api/v1/strategy/pump-paper-v1/stats?window=session&mc=0`

包络 `{ ok, data }`。别名 `GET /api/v1/stats/paper-performance`。`data.mode="paper"`，`liveDisabled=true`。

---

## 5. UI

行情页 / 交易页：摘要条（胜率 / 期望 / 回撤 / 笔数）+ 权益 sparkline + journal。蒙特卡洛勾选默认关；样本不足不画假分位带。

版本：v1。不改冻结事件名 `signal|risk|fill|reject|trading_state`。
