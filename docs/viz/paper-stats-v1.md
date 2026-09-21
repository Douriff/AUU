# 纸面表现统计 v1（成功概率 / 蒙特卡洛）

状态：冻结草稿（2026-09-21）  
模式：**仅纸面 / 模拟**。由已平仓纸面成交重抽样，**不是**实盘胜率承诺，也不是未来收益保证。

对接：`PaperBroker` Fill → session ledger → `GET /api/v1/stats/paper-performance`

---

## 1. 样本

- **成交来源**：所有走 `RiskGate → PaperBroker` 的 Fill（手动 Trade、`decide-and-fill`、`strategy_autopaper` 自动单）。同一条 broker 路径，不另开撮合。
- **平仓笔**：按 symbol FIFO 把反向成交配对成 round-trip。未平仓 lot 不计入胜率。
- **窗口**：默认 `window=session`（进程内存）；`window=N` 取最近 N 笔平仓。可选 `from` / `to`（unix ms）。

空仓 / 尚无平仓：`empty=true`，`win_rate` 等为 `null`。前端显示「暂无平仓样本」。

---

## 2. 公式

记第 \(i\) 笔已平仓交易收益率为 \(r_i = \text{pnl\_pct}\)（扣费后）：

| 多头 | \(r = (P_\text{exit}-P_\text{entry})/P_\text{entry} - \text{fee}/(P_\text{entry} Q)\) |
| 空头 | \(r = (P_\text{entry}-P_\text{exit})/P_\text{entry} - \text{fee}/(P_\text{entry} Q)\) |

| 字段 | 定义 |
|------|------|
| `trade_count` | 已平仓笔数 \(n\) |
| `wins` / `losses` | `pnl > 0` / `pnl < 0` |
| `win_rate` | **成功概率（胜率）** \(= wins / n\)（样本内） |
| `expectancy_pnl_pct` | \(\bar r \times 100\)（平均收益率，百分点） |
| `expectancy_r` | \(\bar r / \text{stop\_loss\_pct}\)（默认 SL=12%，平均 R） |
| `max_drawdown_pct` | 将 \(r_i\) 连乘权益曲线 \(E_0=1,\; E_{t}=E_{t-1}(1+r_t)\)，峰到谷 \((peak-E)/peak \times 100\) |
| `sharpe_like` | \(n \ge 5\) 时 \(\bar r / s_{r}\)（**交易收益样本**，不年化）；样本不足为 `null` |

---

## 3. 蒙特卡洛（可选）

对已实现的 \(\{r_i\}\) **有放回**重抽样 \(n\) 次，重复 `n_paths`（默认 **1000**，`seed` 默认 42）：

1. 每条路径从权益 1.0 连乘
2. `p_equity_positive` = \(P(E_\text{final} > 0)\)
3. `p_equity_above_start` = \(P(E_\text{final} > 1)\)
4. `p_hit_day_loss` = 路径上曾触及 \(E \le 1 - \text{max\_day\_loss\_pct}\)（默认 5%）的比例
5. `final_equity_pct_p5/p50/p95` = 终值相对起点的百分点分位

`monte_carlo.label` / 顶层 `disclaimer` 固定为：

> simulation from paper history, not a promise — 纸面历史重抽样，非实盘承诺

---

## 4. REST

`GET /api/v1/stats/paper-performance?window=session&n_paths=1000&seed=42`

包络 `{ ok, data }`，`X-Api-Version: 1`。`data.mode="paper"`，`liveDisabled=true`。

---

## 5. UI

行情页 / 交易页紧凑面板：成功概率（胜率）、笔数、期望、回撤、蒙特卡洛区间。无平仓时为空态。开关 `strategy_autopaper` / `auto_paper_orders` 默认关，与本统计独立（无自动单也可以看手动纸面单的样本）。

版本：v1。不改冻结事件名 `signal|risk|fill|reject|trading_state`。
