# 可视化 · 可执行性（Go/NoGo）面板规格 v0

对齐：望舒门槛 + `decision-log-v0.md`。实盘仍默认关；本面板给「开 live 前」证据，非跟单。

---

## 1. 位置

Strategy / Stats 区：`ExecutabilityPanel`（可与 PaperStats 同列）。  
顶栏可挂 Go/NoGo 灯（灰=数据不足，绿=过门槛，红=未过）。

---

## 2. 门槛展示（只读，阈值以后端为准）

| 指标 | 过线条件（产品） | UI |
|------|------------------|-----|
| 平仓样本 | n≥30 且 `sample_ok` | 笔数 + sample_ok 徽标 |
| 期望 | ≥0 或「书面接受微负」开关态 | 期望值；微负时黄灯+备注 |
| 入场冲击中位 | `ok` 只在扣费中位 ≥60 或任一含费 >80 时失败（等于 80 过）。短样本是 coverage，总 verdict 仍要 n≥30 | 「扣费」对 <60；「含费」硬顶 >80；样本不足单独标灰 |
| 影子滑点 | 受控（后端字段） | 分布或 P50/P90 |
| 拒单结构 | 可解释 | progress / impact / risk 三桶条形占比 |
| Live 闸 | 限额+本机钥+二次确认 | 三勾只读（与 live-ui-gates 同源） |

Go = 上表均满足；任一缺数据 → NoGo「证据不足」，禁止暗示可开 live。

---

## 3. 数据

- PaperStats / Journal（样本、期望）
- DecisionLog 聚合：`progress_band` / `impact` / risk 桶、预估冲击、影子滑点、outcome
- API：云端落地路径为准（如 `GET .../executability` 或扩展 stats）

---

## 4. 文案

- 标题：「可执行性证据」
- 禁「保证盈利」「跟单成功率」
- NoGo 主因一行人话（如「平仓样本 12/30」）

---

## 5. 切片

P0：面板壳 + Go/NoGo 灯 + 样本/期望/冲击中位空态  
P1：拒单三桶 + 影子滑点接 DecisionLog  
P2：与开 live 二次确认页深链（仍须手动确认）

版本：v0。
