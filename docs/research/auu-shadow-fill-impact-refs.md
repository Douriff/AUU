# AUU「影子成交 / 冲击回放」开源可借鉴清单

> 调研：2026-09-21 23:10 CST（Asia/Shanghai）  
> 仓库：Douriff/AUU · 纸面量化可视化  
> 范围：**影子 fill**（下一根 bar / 下一笔市价回放「若成交会怎样」）、**冲击成本归因**、**implementation shortfall (IS)**、与 **DecisionLog / 可执行性面板** 的字段接法  
> **已有可沿用（不重复深挖）**：`pumpfun_curve_math.estimated_impact_bps`、PaperBroker、DecisionLog（预估冲击 / 影子滑点 / outcome）、Nautilus FillModel 思想、Jesse MC、QuantStats（见此前 landscape / autopaper-stats）  
> 原则：纸面/模拟 only；**非跟单、非实盘密钥**；MIT/Apache **优先**；GPL/AGPL/LGPL **只标思路勿嵌代码**；不确定标「待核实」。  
> Stars 为 GitHub 当日 `gh api` 抓取（会波动）。星数档：`S` ≥20k · `A` 5–20k · `B` 1–5k · `C` <1k。

---

## 0. AUU 对齐锚点（读清单前先看）

### 0.1 现状钩子

| 组件 | 现状 | 本轮要补的证据 |
|------|------|----------------|
| 曲线冲击 | `LiquidityCtx.estimated_impact_bps` / `estimated_curve_impact_bps`（有 `ctx.pump` 走 bonding curve，否则 CEX √） | **预估 vs 影子实现** 的偏差分布 |
| PaperBroker | 限价穿越 / 深度吃单 / 公式滑点；拒单码含 `SLIPPAGE_CAP` | 纸面 Fill 价 ≠ 链上可成交价 → 需 **shadow fill** 对照 |
| DecisionLog | `AutoDecision`：`ts/symbol/action/allow/reason/tags/notes`；策略输出已有 `estimated_impact_bps` | 扩展/旁路字段：`shadow_*`、`is_*`、`outcome` |
| Go/No-Go | 平仓 ≥30；冲击中位 `<60bps`（硬顶 80）；`shadow_slippage` P50/P90 在阈值内 | 面板：`GET .../stats/executability` 聚合 |

### 0.2 推荐 DecisionLog 字段契约（自研，勿拷 GPL）

建议在现有 `AutoDecision` / journal 旁路增加（命名可微调，语义冻结）：

| 字段 | 含义 | 单位 |
|------|------|------|
| `decision_px` / `arrival_px` | 决策/到达价（信号刻 mid 或 curve mid） | SOL/token |
| `estimated_impact_bps` | 下单前曲线/公式冲击（已有） | bps |
| `paper_fill_px` | PaperBroker 实际记账价 | price |
| `shadow_fill_px` | **下一根 bar open/close** 或 **下一笔 tape 价** 回放「若当时按市价吃」的影子价 | price |
| `shadow_slippage_bps` | `sign(side) * (shadow_fill_px - decision_px) / decision_px * 1e4`（卖反向） | bps |
| `impact_error_bps` | `shadow_slippage_bps - estimated_impact_bps`（正=预估偏乐观） | bps |
| `is_complete_bps` / `is_trade_cost_bps` | Complete / Market-Activity IS（见 §2 blotter 公式） | bps |
| `outcome` | `fill \| deny \| skip \| refuse` + 原因桶 `progress \| impact \| risk` | enum |

**面板聚合（可执行性）**

- `median_entry_impact_bps` = `estimated_impact_bps` 在入场 submit/fill 子集上的中位（门槛 `<60`，硬顶 `80`）
- `shadow_slippage` P50/P90（后端阈值；系统性 `impact_error_bps > 0` → No-Go）
- 拒单三桶：`progress_band` / `impact`/`SLIPPAGE_CAP` / 其它 risk

### 0.3 AUU 最小可行 shadow fill（不依赖外仓）

```
t0: Signal / OrderIntent 记下 decision_px, estimated_impact_bps, notional, side
t1: 下一根 1m bar 的 open（或下一笔 tape trade 价）→ shadow_fill_px
    shadow_slippage_bps = f(side, decision_px, shadow_fill_px)
    # 可选：t1 用当时曲线再算 estimated_impact_bps_at_t1 做「延迟冲击」
写入 DecisionLog + 滚动进 executability 面板
```

曲线场景优先：**shadow 用下一笔曲线报价 / tape**，bar 仅作无 tape 时的降级；与 `pumpfun_paper` 1m tape 已存在能力对齐。

---

## 1. 优先：影子成交 / 回放引擎（高星 · MIT/Apache）

### 1.1 hftbacktest — `nkaz001/hftbacktest`

| 字段 | 内容 |
|------|------|
| **星数档** | **B→A 边缘** · ~4.7k |
| **许可证** | **MIT**（优先） |
| **活跃** | 极活跃（2025-12 仍推；Rust+Python；Binance/Bybit crypto 例） |
| **能借什么** | ① **全量 tick / L2·L3 回放**「订单意图 → 队列位置 → 延迟 → fill」；② 可插拔 latency / fill model；③ 同一算法研究→（其 live 仅作对照，AUU **不用**） |
| **为何适合 AUU** | 本轮核心问题是「纸面价 vs 若真实吃流动性会怎样」；hft 的 **latency-aware shadow fill** 是行业标杆。Pump.fun 无经典 LOB 时可借：**延迟 + 参与率 + 部分成交** 的状态机，曲线冲击仍用 AUU 自研 |
| **怎么接到 DecisionLog / 可执行性面板** | **不要整嵌引擎**。抽「意图时间戳 → 延迟 δ → 在 t0+δ 的市价/深度上估价」写成 AUU `ShadowFillModel`：输出 `shadow_fill_px` / `shadow_slippage_bps` 写入 DecisionLog；聚合 P50/P90。有 L2 迁移池后再考虑 queue-position；bonding curve 阶段用曲线 quote @ t0+δ |

### 1.2 QuantConnect LEAN — `QuantConnect/Lean`

| 字段 | 内容 |
|------|------|
| **星数档** | **S** · ~21.7k |
| **许可证** | **Apache-2.0**（优先） |
| **活跃** | 极活跃 |
| **能借什么** | ① `ImmediateFillModel` / 可插拔 Fill；② `VolumeShareSlippageModel(volumeLimit, priceImpact)`：按 bar 量占比估滑点；③ PaperBrokerage = **真行情 + 假成交** 产品定义 |
| **为何适合 AUU** | 与 PaperBroker 插件点同构；`VolumeShareSlippage` 可作 **无深度时的 shadow 降级模型**（参与率 × 冲击系数），对照曲线 `estimated_impact_bps` |
| **怎么接到 DecisionLog / 可执行性面板** | 自研 `volume_share_shadow_bps(notional, bar_volume, k)` → 填 `shadow_slippage_bps`（tag `shadow_src=vol_share`）；与 `estimated_impact_bps` 并列进 executability。**禁止**嵌 C# 引擎 / QC 云密钥 |

### 1.3 Jesse — `jesse-ai/jesse`（此前已沿用 MC）

| 字段 | 内容 |
|------|------|
| **星数档** | **A** · ~8.6k |
| **许可证** | **MIT** |
| **活跃** | 活跃 |
| **能借什么** | `research.monte_carlo`：交易顺序/路径扰动；回测里对 fill/滑点假设做敏感性（详见 `/workspace/jesse-montecarlo-for-auu.md`） |
| **为何适合 AUU** | 影子滑点阈值本身要 **MC 稳健**：冲击中位、shadow P90 是否在种子扰动下仍过 Go |
| **怎么接到 DecisionLog / 可执行性面板** | 对已落盘 DecisionLog 行做 trades-level shuffle：重算 `median_entry_impact_bps` / `shadow_slippage` P90 置信带；**不**改 PaperBroker 成交路径 |

### 1.4 QuantStats — `ranaroussi/quantstats`

| 字段 | 内容 |
|------|------|
| **星数档** | **A** · ~7.7k |
| **许可证** | **Apache-2.0** |
| **活跃** | 维护中 |
| **能借什么** | tear sheet / 滚动指标展示；**不是** fill 引擎 |
| **为何适合 AUU** | 可执行性面板「冲击/影子滑点时间序列 + 分位」可视化模板 |
| **怎么接到 DecisionLog / 可执行性面板** | 把 DecisionLog 导出为「伪收益」序列：`-impact_error_bps` 或 `-shadow_slippage_bps` 日聚合 → QuantStats 风格图；证据层仍以中位/分位门槛为准 |

---

## 2. TCA / Implementation Shortfall（归因公式 · 面板指标）

### 2.1 tcapy — `cuemacro/tcapy`

| 字段 | 内容 |
|------|------|
| **星数档** | **C** · ~251 |
| **许可证** | **Apache-2.0**（优先） |
| **活跃** | 低（末推 ~2024-02；标 alpha）→ **可借概念，落地前待核实可运行性** |
| **能借什么** | FX 向 TCA：arrival / TWAP / VWAP **基准**；自定义 metric 插槽；本地跑、数据不出域 |
| **为何适合 AUU** | 给 DecisionLog 定「基准价」词汇：`arrival_px` vs `decision_px` vs `paper_fill` vs `shadow_fill` |
| **怎么接到 DecisionLog / 可执行性面板** | 映射：`arrival`≈信号刻 curve mid；`benchmark_slippage_bps`；面板分列 estimated / shadow / IS。**勿整装依赖**（重、偏 FX tick） |

### 2.2 blotter · `impShortfall` — `braverock/blotter`（R）

| 字段 | 内容 |
|------|------|
| **星数档** | **C** · ~116 |
| **许可证** | GitHub SPDX 空 → 生态常为 GPL 系 → **公式/思路勿嵌 R 源码**（待核实 SPDX） |
| **活跃** | 低维护 |
| **能借什么（公式）** | Perold / Wagner / Market Activity IS 分解：  
  · **Complete**：`IS ≈ S·(P_avg − P_d) + fees`  
  · **Perold**：`Σ sⱼ(P_avg−P_d) + (S−Σsⱼ)(P_n−P_d) + fees`  
  · **Wagner**：Delay `S(P_0−P_d)` + Trade `Σsⱼ(P_avg−P_0)` + Opp `(S−Σsⱼ)(P_n−P_0)`  
  · **Market**：不含决策延迟，只用 arrival `P_0` |
| **为何适合 AUU** | 把「冲击」从单一 bps 拆成 **delay / trade / opportunity**，解释 No-Go |
| **怎么接到 DecisionLog / 可执行性面板** | 自研 Python：`is_method=wagner`；`P_d=decision_px`，`P_0=arrival`（可=下一笔可见 mid），`P_avg=paper_fill` 或 `shadow_fill`（两列对照），`P_n=窗口末日价`。面板展示 `delay_bps / trade_bps / opp_bps` |

### 2.3 equity-tca — `rosesparrow/equity-tca`

| 字段 | 内容 |
|------|------|
| **星数档** | **C** · 0 |
| **许可证** | **未声明** → 待核实；默认只读思路 |
| **活跃** | 2026-03 有推 |
| **能借什么** | Streamlit TCA 仪表盘：IS 主指标、策略对比（Immediate/VWAP/…）、订单规模×时机 |
| **为何适合 AUU** | 可执行性面板信息架构（筛选、分位、策略对照）参考 |
| **怎么接到 DecisionLog / 可执行性面板** | 只借 UI 分区：样本 n、冲击中位、shadow P90、拒单三桶；实现用 AUU stats API |

### 2.4 execution-tca-lab — `ely2ba/execution-tca-lab`

| 字段 | 内容 |
|------|------|
| **星数档** | **C** · 1 |
| **许可证** | **MIT** |
| **活跃** | 2026-06 |
| **能借什么** | LOBSTER 回放 + VWAP 模拟；**IS / VWAP slippage / fill rate**；L5 walk 校验「假设冲击 κ 是否与簿深一致」；CUSUM 监控 IS 漂移 |
| **为何适合 AUU** | 「预估冲击 vs 可执行深度」审计 = AUU `estimated_impact_bps` vs shadow/曲线深度的科学对照；CUSUM 可作 shadow_slippage 告警 |
| **怎么接到 DecisionLog / 可执行性面板** | 借 **impact audit**：对 fill 子集算 `κ_fit` vs 曲线模型；`impact_error_bps` 滚动 CUSUM → 面板「冲击模型失真」灯。无 LOBSTER 时用 pump tape/curve 替代 |

### 2.5 End-to-End … TCA-Dashboard — `Rohan473/End-to-End-Market-Data-Pipeline-TCA-Dashboard`

| 字段 | 内容 |
|------|------|
| **星数档** | **C** · 0 |
| **许可证** | **未声明** → 待核实 |
| **能借什么** | `tca.py`：IS、VWAP slippage、participation、半价差、√(q/ADV) 冲击；TWAP/VWAP 切片模拟；t 检验 / bootstrap |
| **为何适合 AUU** | 统计检验「shadow 与 estimated 是否系统性偏离」→ Go/No-Go 证据强度 |
| **怎么接到 DecisionLog / 可执行性面板** | 对 `impact_error_bps` 做 bootstrap CI；`n_closed≥30` 时报告是否拒绝「误差中位=0」 |

### 2.6 TRACE-ZERO — `Dharshan2004/trace-zero`

| 字段 | 内容 |
|------|------|
| **星数档** | **C** · 1 |
| **许可证** | **MIT** |
| **能借什么** | Almgren–Chriss vs VWAP/TWAP/Dump；`IS_bps = (P_arrival − VWAP_fills)/P_arrival × 1e4`；延迟 `latency_ms` 使簿过期 → IS 上升 |
| **为何适合 AUU** | 明确 **latency → shadow 变差** 的可复现实验设计（对齐时延条目 Go/No-Go） |
| **怎么接到 DecisionLog / 可执行性面板** | 参数扫描 `latency_ms ∈ {0,250,500,1000}` 重放 shadow；面板展示「时延敏感曲线」；不嵌其全栈 UI |

---

## 3. 冲击模型 / 曲线外解析式（对照 `estimated_impact_bps`）

### 3.1 Almgren–Chriss 参考实现 — `joshuapjacob/almgren-chriss-optimal-execution`

| 字段 | 内容 |
|------|------|
| **星数档** | **C** · ~48 |
| **许可证** | **MIT** |
| **活跃** | 停更（2022）→ 公式仍可用 |
| **能借什么** | 最优轨迹 / 期望成本；临时+永久冲击分解思想 |
| **为何适合 AUU** | bonding curve 已有精确冲击时，AC 作 **CEX/迁移后** 对照；或把曲线冲击拆「临时 vs 永久」叙事 |
| **怎么接到 DecisionLog / 可执行性面板** | 可选字段 `model=curve|ac_sqrt`；**默认仍 curve**。面板禁止把 AC 成本误标为已实现 |

### 3.2 wickra-impact — `wickra-lib/wickra-impact`

| 字段 | 内容 |
|------|------|
| **星数档** | **C** · 1（新） |
| **许可证** | **MIT OR Apache-2.0**（优先） |
| **活跃** | 2026-09-20 仍推 |
| **能借什么** | L2 **orderbook walk** fill；无簿时 `linear_impact` / `square_root`；`participation_cap`；`latency_ms` 选哪本簿 |
| **为何适合 AUU** | 与「影子成交=测而非猜」同题；√ 冲击与 AUU CEX 分支同族 |
| **怎么接到 DecisionLog / 可执行性面板** | 借 `participation_cap` + latency 选价逻辑进 `ShadowFillModel`；曲线阶段用 `buy_tokens_out`/`sell_sol_out` 当「walk」。星少 → PoC 级，待核实 API 稳定 |

### 3.3 trade-sim — `tashifkhan/trade-sim`

| 字段 | 内容 |
|------|------|
| **星数档** | **C** · 0 |
| **许可证** | **MIT** |
| **能借什么** | 简化 AC：`Impact = σ · √(Q/V) · C`；L2 成本分解 UI（偏演示） |
| **为何适合 AUU** | 与无 `ctx.pump` 时 CEX √ 分支同形，可作单元测试对照 |
| **怎么接到 DecisionLog / 可执行性面板** | 仅作 `estimated_impact_bps` 回归基线；**不**替代 curve |

---

## 4. 代理仿真 / 「冲击实验」设计（思路为主）

### 4.1 ABIDES — `jpmorganchase/abides-jpmc-public`（及历史 `abides-sim/abides`）

| 字段 | 内容 |
|------|------|
| **星数档** | JPMC 公开仓 ~174（**已 archived**）；原仓 ~571 |
| **许可证** | BSD-3（文献/README）· GitHub SPDX 多为 NOASSERTION → **待核实后引用** |
| **能借什么** | 多代理 LOB；论文中用 **ImpactAgent** 对照基线测市场冲击；延迟矩阵 |
| **为何适合 AUU** | 「有/无大单」对照实验设计 → 验证 shadow 是否高估/低估 |
| **怎么接到 DecisionLog / 可执行性面板** | **勿嵌模拟器**。借实验设计：同一 DecisionLog 意图，分别用 `estimated` / `next_bar` / `next_trade` / `curve_quote@t+δ` 四路 shadow，报告冲击中位差 |

### 4.2 MidasTrader — `midassystems/midastrader`

| 字段 | 内容 |
|------|------|
| **星数档** | **C** · 6 |
| **许可证** | **Apache-2.0** |
| **活跃** | 一般（2025-06） |
| **能借什么** | 回测/实盘同码切换、订单簿组件；**无专用冲击模型**（搜索结论） |
| **为何适合 AUU** | 弱相关；仅作「paper/live 同路径」产品对照（AUU 已有且更严） |
| **怎么接到 DecisionLog / 可执行性面板** | 低优先；不接 |

---

## 5. 可检视成交路径 / 小而精（MIT，星低但贴题）

### 5.1 ordersim — `tradingexpert/ordersim`

| 字段 | 内容 |
|------|------|
| **星数档** | **C** · 2 |
| **许可证** | **MIT** |
| **活跃** | 2026-08 活跃 |
| **能借什么** | **可检视** 订单簿回放 + 延迟 fill；强调「意图→成交路径」而非只报 PnL；Python 参考引擎 |
| **为何适合 AUU** | DecisionLog 要的正是可审计路径；比 hftbacktest 轻、可读 |
| **怎么接到 DecisionLog / 可执行性面板** | 对照其「每事件记账」：AUU 每笔 intent 落 `decision→shadow→paper→outcome` 四元组；面板可下钻单笔 |

### 5.2 realistic-mm-backtester — `tfrmma/realistic-mm-backtester`

| 字段 | 内容 |
|------|------|
| **星数档** | **C** · 9 |
| **许可证** | **MIT** |
| **能借什么** | FIFO 队列、延迟、adverse selection 指标；Pro 引擎更真 |
| **为何适合 AUU** | 迁移后 AMM/CLMM 或 CEX 阶段的被动单；当前 bonding curve **低优先** |
| **怎么接到 DecisionLog / 可执行性面板** | 远期：`shadow_src=queue`；现在只记思路 |

### 5.3 flashalpha-fill-simulator — `FlashAlpha-lab/flashalpha-fill-simulator`

| 字段 | 内容 |
|------|------|
| **星数档** | **C** · 4 |
| **许可证** | **MIT** |
| **能借什么** | 期权价差限价 fill 概率；engine-agnostic |
| **为何适合 AUU** | 弱域相关；可借「fill 概率 vs 必成」对照 PaperBroker 乐观假设 |
| **怎么接到 DecisionLog / 可执行性面板** | 可选 `prob_fill` 写入 notes；非本轮主线 |

---

## 6. 此前 landscape 已点名 · 本轮仅补「影子/冲击」接法

### 6.1 NautilusTrader FillModel — `nautechsystems/nautilus_trader`

| 字段 | 内容 |
|------|------|
| **星数档** | **S** · ~29.2k |
| **许可证** | **LGPL-3.0** → **思路勿嵌**（动态链接/插件边界待法务；AUU 惯例：只借概念） |
| **能借什么** | `prob_fill_on_limit` / `prob_slippage`（L1 一跳）；L2/L3 由簿深决定冲击；`random_seed` 可复现；`liquidity_consumption` |
| **为何适合 AUU** | 已有适配草图 `docs/riskgate-paperbroker-v0.md`；补 shadow：L1 概率滑点 = DecisionLog 的 **随机影子** 对照（与确定性 next-bar 影子并列） |
| **怎么接到 DecisionLog / 可执行性面板** | 自研等价：`shadow_slippage_bps` 两列 `next_bar` + `l1_prob`；seed 写入 DecisionLog 便于复现。**禁止**把 LGPL 源码拷进主仓 |

### 6.2 backtrader / Freqtrade / OctoBot 等 GPL

| 项目 | 星数约 | 许可 | 本轮可借思路（勿嵌） |
|------|--------|------|----------------------|
| backtrader | ~23.3k | GPL-3.0 | `TradeOnNextOpen` / 滑点 perc：即 AUU next-bar shadow |
| Freqtrade | ~54.6k | GPL-3.0 | 费率+滑点假设分层；dry-run 与 live 切换叙事 |
| OctoBot | ~6.6k+ | GPL | Simulator 开关产品形态 |

**怎么接到面板**：只把「下一根 open 成交」写成 AUU 自研 `shadow_src=next_open`；不 `pip install` 进主依赖。

### 6.3 VectorBT — `polakowo/vectorbt`

| 字段 | 内容 |
|------|------|
| **星数档** | **A** · ~9.1k |
| **许可证** | Apache-2.0 + **Commons Clause**（NOASSERTION）→ **商用分发待核实；研究可参考 API** |
| **能借什么** | `fees`/`slippage` 向量化；快速扫参 |
| **怎么接** | 离线扫 `slippage` 网格 vs AUU curve impact；结果导入面板作敏感性，不嵌 runtime |

---

## 7. 推荐落地顺序（可执行性证据）

| 优先级 | 动作 | 证据落点 | 主要借鉴 |
|--------|------|----------|----------|
| **P0** | 实现 `shadow_src=next_trade\|next_open`，写 DecisionLog | `shadow_slippage_bps` P50/P90 | backtrader 思路 / LEAN Immediate + 自研 |
| **P0** | `impact_error_bps = shadow − estimated` | 模型乐观偏差灯 | tcapy 基准词 / execution-tca-lab audit |
| **P0** | 聚合 `median_entry_impact_bps`（submit/fill 子集） | Go `<60` / 硬顶 `80` | 已有 curve math |
| **P1** | Wagner/Perold IS 分解（自研 Python） | delay/trade/opp 三列 | blotter 公式（勿嵌 R） |
| **P1** | `latency_ms` 扫描重放 shadow | 时延敏感曲线 | TRACE-ZERO / hftbacktest 思想 |
| **P2** | bootstrap / CUSUM on `impact_error` | 统计 No-Go | TCA-Dashboard / execution-tca-lab |
| **P2** | Jesse MC 扰动 DecisionLog | 门槛稳健带 | Jesse MIT |
| **P3** | L2 walk / queue（迁移后） | `shadow_src=book_walk` | hftbacktest / wickra / ordersim |

---

## 8. 明确不做

- 不镜像跟单、不接实盘密钥 / `sendTransaction` 作为本清单验收
- 不把纸面 Fill 直接标成「可执行」；缺 shadow / 冲击中位证据 → **No-Go（证据不足）**
- 不整嵌 hftbacktest / LEAN / ABIDES / Nautilus 作为 AUU runtime
- GPL/LGPL：**思路与公式自研重写**，不复制源码进主仓

---

## 9. 待核实清单

| 项 | 原因 |
|----|------|
| `braverock/blotter` SPDX | GitHub license 空；公式可引用，嵌码风险未知 |
| `rosesparrow/equity-tca` / `Rohan473/...TCA-Dashboard` 许可 | 未声明 |
| `tcapy` 可运行性 | 末更 2024、依赖重 |
| `wickra-impact` / `ordersim` API 稳定 | 星极少、项目新 |
| ABIDES SPDX / archived 分叉选哪个 | JPMC public 已归档 |
| VectorBT Commons Clause 对 AUU 分发 | 法务 |
| Nautilus LGPL 动态链接边界 | 法务；当前仅思想 |

---

## 10. 版本

v0 · 2026-09-21 23:10 CST · 影子成交/冲击回放专项 · 供父代理与望舒 Go/No-Go 对齐
