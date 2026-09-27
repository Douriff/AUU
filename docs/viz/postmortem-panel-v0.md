# 可视化 · 平仓后推演报告面板 v0（无 LLM）

与 `ExecutabilityPanel` / PaperStats 同列：`PostmortemPanel`。标题：**平仓后推演**（副文：「规则聚合 · 非大模型」）。

布局：摘要（n_closed / expectancy / win_rate / Exec）→ 出场占比条 + 冲击分位表 → findings（最多 5 条）。空态「样本累积中（x/30）」不改 Go 灯。`overall_go=false` 时不展示实盘入口。无改参按钮。

REST：`GET /api/v1/strategy/pump-paper-v1/postmortem`。
