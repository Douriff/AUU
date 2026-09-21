# AUU 频道摘要 · 影子成交 / 冲击回放开源清单

> 2026-09-21 23:10 CST · 完整版：`docs/research/auu-shadow-fill-impact-refs.md`  
> 范围：影子 fill + 冲击归因 + IS；非跟单、非实盘密钥。MIT/Apache 优先；GPL/LGPL 只思路。

## 一句话

在已有 `estimated_impact_bps` + PaperBroker + DecisionLog 上，补 **下一笔/下一根 bar 的 shadow fill**，用 `shadow_slippage` 与冲击中位做可执行性证据（平仓≥30、中位&lt;60bps、硬顶80）。

## DecisionLog 怎么接（建议字段）

| 已有/目标 | 用途 |
|-----------|------|
| `estimated_impact_bps` | 下单前曲线/公式冲击（已有） |
| `shadow_fill_px` + `shadow_slippage_bps` | t0 决策后，用下一笔 tape 或下一根 open 回放「若成交」 |
| `impact_error_bps` | shadow − estimated（&gt;0 = 预估偏乐观 → No-Go 信号） |
| `is_*`（Complete/Wagner） | delay / trade / opportunity 分解 |
| `outcome` + 拒单三桶 | fill/deny + progress/impact/risk |

面板：`median_entry_impact_bps`、`shadow_slippage` P50/P90、`n_closed`。

## 优先借鉴（高星 / 许可友好）

| 项目 | 星≈ | 许可 | 借什么 |
|------|-----|------|--------|
| **LEAN** | 21.7k | Apache | Fill/Slippage 插件；`VolumeShareSlippage` 作无深度降级 shadow |
| **hftbacktest** | 4.7k | MIT | 延迟感知回放；抽「t0+δ 估价」状态机，勿整嵌 |
| **Jesse** | 8.6k | MIT | MC 扰动 DecisionLog，门槛稳健带（已沿用） |
| **QuantStats** | 7.7k | Apache | 冲击/影子分位可视化，非 fill 引擎 |
| **tcapy** | 0.25k | Apache | arrival/VWAP 基准词；偏旧，概念用 |
| **execution-tca-lab** | 1 | MIT | IS + 冲击 κ 审计 + CUSUM（贴题，星低） |
| **TRACE-ZERO** | 1 | MIT | latency→IS 实验设计 |
| **ordersim** | 2 | MIT | 可检视意图→成交路径 |
| **wickra-impact** | 1 | MIT/Apache | book walk / √冲击 / participation_cap |
| **almgren-chriss (joshuapjacob)** | 48 | MIT | AC 公式对照（曲线仍为主） |

## 只借思路（勿嵌代码）

- **Nautilus FillModel**（~29k，**LGPL**）：prob_slippage / seed — 已有适配草图  
- **blotter `impShortfall`**：Perold/Wagner 公式 — SPDX 待核实，自研 Python  
- **backtrader / Freqtrade（GPL）**：next-bar fill = AUU `shadow_src=next_open`  
- **ABIDES**（archived/分叉）：ImpactAgent 对照实验设计，勿嵌模拟器  

## 建议落地序

1. **P0** `next_trade|next_open` shadow → DecisionLog → P50/P90  
2. **P0** `impact_error` + 冲击中位进 executability  
3. **P1** Wagner IS 分解 + `latency_ms` 扫描  
4. **P2** bootstrap/CUSUM + Jesse MC  
5. **P3** 迁移后 L2 walk（hft/wickra/ordersim）

## 不做

整嵌重型引擎；把纸面 Fill 当可执行；缺 shadow/中位证据却暗示可开 live。

## 待核实

blotter/部分 TCA demo 许可；tcapy 可运行性；wickra/ordersim API；VectorBT Commons Clause；Nautilus LGPL 边界。
