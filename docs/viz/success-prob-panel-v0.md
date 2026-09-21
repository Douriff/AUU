# 可视化 · 成功概率 / 纸面统计面板规格 v0

对齐：autopaper 闭环（默认关）；PaperTradeJournal + `GET .../stats`（策略建议）；可选 MC 借 Jesse 思路。纸面 only。

---

## 1. 位置

Market / Strategy 右侧或底栏：`PaperStatsPanel`（可折叠，默认展开摘要一行）。

与盘面并存：不挡 K 线；autopaper 灯旁可挂「胜率 xx%」。

---

## 2. 布局（预留）

```
┌─ PaperStats ─────────────────────────┐
│ 胜率  期望R  回撤  笔数  autopaper灯   │  ← 摘要条
├──────────────────────────────────────┤
│ EquityCurve（净值）                   │
│ DrawdownStrip                         │
│ 最近 round-trip 表（entry/exit/pnl）  │
│ [可选] MonteCarlo 分布（P50/P05）     │
└──────────────────────────────────────┘
```

Settings：`show_paper_stats` 默认开；`show_monte_carlo` 默认关（有 API 再亮）。

---

## 3. 数据合同（跟策略 stats，字段可追加）

```ts
PaperStats {
  n_trades: number
  win_rate: number          // 0–1
  expectancy: number        // 每笔期望 pnl（报价币）
  max_drawdown_pct: number
  equity: { t: number, equity: number }[]
  journal: {
    id, symbol, entry_ts, exit_ts,
    entry_price, exit_price, pnl, tags[]
  }[]
  monte_carlo?: {
    n_paths: number
    p05_pnl: number
    p50_pnl: number
    p95_pnl: number
  }
}
```

REST：`GET /api/v1/strategy/pump-paper-v1/stats`（路径以 AUU 落地为准）。  
无数据：空态「暂无已平仓纸面交易」。

---

## 4. 行为

- autopaper 关：仍可展示历史 journal（若有）；不暗示实盘。
- 拒单 / 未平仓：不进胜率分母（跟 journal 定义）。
- MC：仅展示后端结果，前端不算路径。

---

## 5. 切片

P0：摘要条 + 空态 + 类型预留  
P1：权益/回撤 + journal 表接 stats  
P2：MC 区块（`show_monte_carlo`）

版本：v0。

---

## 6. 对齐 Journal/stats（频道）

数据跟 `paper-trade-journal-stats-v0.md`：
- 摘要：胜率 / 期望 / 回撤 / 笔数 ← `GET .../stats`
- `?mc=1`：分位带；`sample_ok=false`（如 n<20）时 MC 区显示「样本不足」不画假带
- 手动 Trade + autopaper 成交均入 journal
