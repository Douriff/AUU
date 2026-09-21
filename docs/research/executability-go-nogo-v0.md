# 纸面成交 → 链上可执行性：Go / No-Go v0

状态：冻结草稿（2026-09-21）  
模式：**仅纸面证据**。本页与 `GET /api/v1/stats/executability` 对齐。  
**不**发链上交易、**不**读私钥、**不**把 `liveEnabled` 置 true。  
对齐：`docs/strategies/pump-paper-v1.md` · `docs/riskgate-paperbroker-v0.md` · `docs/adapters/paper-trade-journal-stats-v0.md`

---

## 0. 要回答的问题

纸面 journal 里有 Fill，只说明 **PaperBroker 记了一笔**。上实盘前需要 **DATA + THEORY** 说明：同样的入场，在 Pump.fun bonding curve 上是否 **可能成交**，而不是“订单对象存在”。

本栈产出的是 **纸面可执行性证据**（`verdict=go|no-go`）。  
即使 `verdict=go`，`liveEnabled` **仍为 false**，直到用户二次确认 **并且** 遵守冻结的 `LiveLimits`（见 §5）。本仓库本轮 **不**打开该开关。

---

## 1. 理论：四类缺口（纸面填不满）

### 1.1 Bonding-curve 冲击

Pump.fun 曲线是虚拟储备恒定乘积（`price = virtual_sol / virtual_token`）。纸面 `LiquidityCtx.estimated_impact_bps`（有 `ctx.pump` 时）走 `pumpfun_curve_math`：

- buy：费后 SOL 进曲线 → `buy_tokens_out`；avg vs mid0 与 mid 移动取 max，再加 `fee_bps/2`（默认 125）
- sell：与 buy **分叉**（`sell_sol_out`），不得共用一支
- `complete` / `migrated` **不**进 CP 公式；近毕业（`progress_bps ≥ 9500` 或 complete）RiskGate 将 impact **×1.5** 再闸

因此：

- 名义越大、曲线越浅（低进度或薄虚拟储备），冲击非线性上升。
- 策略硬顶仍是 `max_impact_bps = 80`（pump-paper-v1 冻结；蒸馏不得抬高）。可执行性 **中位** 更严：入场 `estimated_impact_bps` 中位数 **< 60**。任一入场 **> 80** → 硬顶失败（80 与策略 `<= max_impact_bps` 对齐）。

纸面 CEX 平方根冲击（无 `ctx.pump`）**不能**当作 Pump 可执行性证据；聚合时仍记账，但 `curve_quote_ok` 需要当时有曲线报价（`tick.mid` / `price_sol`）。

### 1.2 延迟

| 层 | 纸面 | 链上（未建模） |
|----|------|----------------|
| 决策→成交 | `PaperBroker.latency_ms=300`，**同一 ctx 报价**（回测不喂未来） | ~400ms slot + 排队 + 落地 |
| 价格 | 成交价 = 决策时 mid ± 公式滑点 | 决策后曲线已被别人买/卖 |

纸面 300ms 只推迟时间戳，**不**换价。影子滑点（§2）是「纸面成交价 vs 当时曲线报价」的下界；实盘延迟只会更差。不得用纸面延迟当作 Jito/优先费已覆盖的证据。

### 1.3 MEV / 三明治

Bonding-curve swap 可被夹（先买后卖）。纸面 Fill：

- **无**对手方流、**无** bundle、**无** sandwich 模拟
- RiskGate `mev_guard` 只把 oversized `urgency=high` 降为 `normal`，**不是** MEV 防护

因此 MEV 是 **理论残差**：本 API **不**声称三明治安全。Go 只表示冲击/样本/影子滑点过门，不表示链上抢块安全。禁止把 `slippage_bps` 解读为 MEV 损失。

### 1.4 毕业风险

- 入场窗冻结：`800 ≤ progress_bps ≤ 7500`；毕业/迁移（`complete`/`migrated`）禁止新开
- 持仓在 `progress_bps ≥ 9000` 或 complete 时强制平仓（拥挤）
- 毕业后 venue 变为 PumpSwap AMM，**曲线纸面成交不能**当作 AMM 可执行

`progress_band` / `not_curve` 拒单是健康过滤，不是故障。可执行性要求把该原因 **报出来**（见 §2），不要求拒单率低于某阈值。

---

## 2. Go / No-Go 门（锁）

分母除非另注，均为 **本会话已平仓纸面 round-trip**（`PaperTradeJournal`）与 **策略入场评估**（`pump-paper-v1` monitor evaluate + RiskGate deny）。手动 Trade 与 autopaper 同一 journal。

`sample_ok`（本页）= `n_trades ≥ 30`。  
纸面胜率面板仍用 `n_trades ≥ 20`（`docs/viz/paper-stats-v1.md`）；**不要混用**。

| # | 门 | Go | No-go | 数据 |
|---|----|----|-------|------|
| G1 | 样本 | `n_trades ≥ 30` 且 `sample_ok=true` | 不足 30 笔已平仓 | journal `closed` |
| G2 | 期望 | `expectancy ≥ 0`（报价币 / 笔） | 均值为负 | `mean(pnl)`；容差见 §2.1 |
| G3 | 入场冲击 | 入场 `estimated_impact_bps` **中位 < 60** | 中位 ≥ 60，或任一样本 **> 80**（硬顶） | 开仓 Fill 上的曲线/公式冲击 |
| G4 | 拒单结构 | `progress` / `impact` / `risk` **三项均已报告**（DecisionLog） | 缺字段 | `docs/adapters/decision-log-v0.md` |
| G5 | 影子滑点 | 中位 `shadow_slippage_bps ≤ X`，`X = 40` | 中位 > 40，或无报价样本 | DecisionLog replay（next_trade\|next_open）或成交当时曲线报价；见 `docs/research/shadow-fill-v0.md` |
| G6 | 实盘开关 | **永不**由本栈置 true | 任何 `liveEnabled=true` 都是违规 | 恒 `false`；见 §5 |

**总 verdict** = G1∧G2∧G3∧G4∧G5。G6 **不**并入 `verdict`：即使纸面 go，`liveEnabled` 仍 false。

### 2.1 期望容差（文档化）

默认 `expectancy_tolerance = 0`。  
**不允许**静默把门槛改成负数。若未来要容忍小幅负期望，必须改本文件并写明：币种、窗口、最大负值。本 v0 **没有**已批准的负容差。

### 2.2 影子滑点 X

```text
shadow_slippage_bps = |fill.price − quote| / quote × 1e4
quote = 成交当时 ctx.tick.mid（pumpfun_paper 上即曲线 price_sol）
```

- `X = 40` bps（中位硬门）。PaperBroker `base_bps=15` 的公式滑点应落在此内；超过说明纸面成交已大幅偏离当时报价，链上只会更差。
- 缺 `quote` 的成交不进入中位；若有效样本 < `min(n_trades, 30)` 且 < 30 → G5 no-go（证据不足）。
- **不是** MEV；**不是** `estimated_impact_bps`（冲击是曲线行走，影子滑点是成交价 vs 决策报价）。

### 2.3 拒单分桶

入场评估（无持仓）每 tick 计 1 次。持仓 hold/出场不计分母。

| 桶 | 策略 `reason` / 标签 |
|----|----------------------|
| `progress` | `progress_band`、`not_curve` |
| `impact` | `impact`；标签 `SLIPPAGE_CAP`、`DEPTH_THIN`（`SPREAD_TOO_WIDE` 同流动性/冲击） |
| `risk` | `blocked_tag`、`cooldown`、`reject_cooldown`、`max_open_mints`、`LIVE_DISABLED`、`TRADING_HALTED`、`REDUCE_ONLY`、`DAY_LOSS_BREAKER`、`HONEYPOT_FLAG`、`TAX_HIGH`、`POSITION_CAP`、`COOLDOWN`、`CURVE_NEAR_GRADUATION`（作为拒单闸） |
| `other` | 如 `momentum`（报告但不作为 G4 缺项） |

```text
reject_rate[bucket] = count(bucket) / n_entry_evals
```

`auto_paper_orders=false` 的 skip **不**记入拒单（用户开关，不是市场不可执行）。G4 只要求三桶 **出现在响应里**（可全 0）；不设拒单率上限。高 `progress_band` 是预期。

---

## 3. HTTP

`GET /api/v1/stats/executability` → `{ ok, data }`。

- 只读聚合。无私钥、无 `sendTransaction`、无 wallet 字段。
- `liveEnabled` 恒为 `false`；`live_limits` 回显冻结上限（§5），**不得放宽**。
- 查询参数可选 `window`（与 paper-performance 相同：`session` / 最近 N 笔）。

响应要点（字段名冻结，可追加）：

```text
verdict: "go" | "no-go"
sample_ok, n_trades, expectancy
median_entry_impact_bps, hard_max_impact_bps=80, impact_cap_go_bps=60
reject_rate: { progress, impact, risk }   # { count, rate }
shadow_slippage: { p50_bps, p90_bps, median_bps, x_bps=40, n, ok }
impact_error: { p50_bps, p90_bps, n }
liveEnabled: false
live_limits: { max_notional_sol, max_day_loss_pct, max_open_mints }
gates: { sample_ok, expectancy, median_entry_impact, reject_rate, shadow_slippage, live }
theory_ref: docs/research/executability-go-nogo-v0.md
```

`gates.live.ok` 恒 false，原因：`liveEnabled remains false until user secondary confirm + LiveLimits`。

---

## 4. 与 PaperStats / 盘面

- Journal 胜率 / MC **不变**（`sample_ok≥20`）。本页是另一条证据。
- 盘面 `estimated_impact_bps` 与成交上记录的入场冲击同源公式。
- UI：PaperStats 下「可执行性」摘要（中文标签）；不提供实盘按钮。

---

## 5. LiveLimits（暗；冻结；禁止放宽）

实盘适配器即使存在也必须 **default off**。放宽 = 增大下列任一项（更大名义、更亏得起、更多同时 mint）：

| 字段 | 冻结上限 | 相对纸面 |
|------|----------|----------|
| `max_notional_sol` | **1** | 纸面策略默认 0.5；实盘不得高于 1 |
| `max_day_loss_pct` | **0.045** | 严于纸面 0.05 |
| `max_open_mints` | **10** | 纸面同时持仓默认 3；实盘上限 10，不得再抬 |

二次确认（本轮 **不实现打开**）：

1. 本地 keypair **仅挂载**（从不入库、不进本 API）
2. 用户二次确认
3. `liveEnabled=true`（本栈永不写入）
4. 订单受 `LiveLimits` 约束

缺任一项 → 拒单标签 `LIVE_DISABLED`。本证据栈 **只读报告** 这些上限，不发送、不签名。

---

## 6. 验收

- [x] 理论：冲击 / 延迟 / MEV / 毕业 写明纸面缺口
- [x] 门：30 笔、期望≥0、中位冲击&lt;60（硬顶 80）、三桶拒单率、影子滑点≤40bps、`liveEnabled=false`
- [x] `GET /api/v1/stats/executability` 无密钥、无链上 send
- [x] 聚合器单测 + fixtures
- [x] 不打开 live、不放宽 LiveLimits、不改 `max_impact_bps` 硬顶 80

版本：v0。只追加门，不改已锁阈值，除非另开 RFC。
