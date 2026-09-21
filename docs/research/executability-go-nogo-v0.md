# 纸面成交 → 链上可执行性：Go / No-Go v0

状态：冻结草稿（2026-09-21）；**入场冲击 Go 口径改为扣协议费**（用户选择 2026-09-22）  
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
- 纸面入场默认 `max_impact_bps = 75`（硬顶 80 下的缓冲）。含费冲击 **> 80** 一律拒单（`GROSS_IMPACT_HARD`）；蒸馏不得把冲击上限抬过 80。Go 硬顶看 **含费** `impact_gross_bps`：任一入场 **> 80** 失败，等于 80 仍过。75 的缓冲不参与这道 Go 门。
- **Go 中位看扣费**（用户选择 2026-09-22）：`impact_net_bps = max(0, impact_gross_bps − protocol_fee_bps)`，中位 **< 60**。`impact_gross_bps` 就是现有 `estimated_impact_bps`（曲线行走 + 费地板）。见 §2.4。

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

- 入场窗（纸面默认，paper round 4）：`1200 ≤ progress_bps ≤ 6500`；毕业/迁移（`complete`/`migrated`）禁止新开。指标与参数见 §7。
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
| G3 | 入场冲击 | 入场 `impact_net_bps` **中位 < 60**（扣协议费） | 扣费中位 ≥ 60，或任一 **含费** 样本 **> 80**（硬顶） | 开仓 Fill 上的曲线/公式冲击；费地板见 §2.4 |
| G4 | 拒单结构 | `progress` / `impact` / `risk` **三项均已报告**（DecisionLog） | 缺字段 | `docs/adapters/decision-log-v0.md` |
| G5 | 影子滑点 | 中位 `shadow_slippage_bps ≤ X`，`X = 40` | 中位 > 40，或无报价样本 | DecisionLog replay（next_trade\|next_open）或成交当时曲线报价；见 `docs/research/shadow-fill-v0.md` |
| G6 | 实盘开关 | **永不**由本栈置 true | 任何 `liveEnabled=true` 都是违规 | 恒 `false`；见 §5 |

**总 verdict** = G1∧G2∧G3∧G4∧G5。G6 **不**并入 `verdict`：即使纸面 go，`liveEnabled` 仍 false。

### 2.1 期望容差（文档化）

默认 `expectancy_tolerance = 0`。  
**不允许**静默把门槛改成负数。若未来要容忍小幅负期望，必须改本文件并写明：币种、窗口、最大负值。本 v0 **没有**已批准的负容差。

### 2.2 影子滑点 X

```text
shadow_slippage_bps = sign(side) * (shadow_fill_px − decision_px) / decision_px × 1e4
# buy/long: sign=+1；sell/short: sign=-1
decision_px = 信号刻 curve mid（ctx.tick.mid / price_sol）
shadow_fill_px = 下一笔曲线 tape 价（优先）或下一根 1m open
impact_error_bps = shadow_slippage_bps − estimated_impact_bps
```

- `X = 40` bps（中位硬门）。缺 replay 时退回成交当时 `tick.mid`（`fill_quote`）。
- 缺有效样本 < `min(n_trades, 30)` 且 < 30 → G5 no-go（证据不足）。
- **不是** MEV；**不是** `estimated_impact_bps`（冲击是曲线行走，影子滑点是决策价 vs 下一笔/下一根可成交价）。
- 公式与落地见 `docs/research/shadow-fill-v0.md`；开源借鉴清单（勿嵌 GPL/LGPL）见 `docs/research/auu-shadow-fill-impact-refs.md`。

### 2.3 拒单分桶

入场评估（无持仓）每 tick 计 1 次。持仓 hold/出场不计分母。

| 桶 | 策略 `reason` / 标签 |
|----|----------------------|
| `progress` | `progress_band`、`not_curve` |
| `impact` | `impact`；标签 `SLIPPAGE_CAP`、`DEPTH_THIN`（`SPREAD_TOO_WIDE` 同流动性/冲击） |
| `risk` | `blocked_tag`、`cooldown`、`reject_cooldown`、`max_open_mints`、`LIVE_DISABLED`、`TRADING_HALTED`、`REDUCE_ONLY`、`DAY_LOSS_BREAKER`、`HONEYPOT_FLAG`、`TAX_HIGH`、`POSITION_CAP`、`COOLDOWN`、`CURVE_NEAR_GRADUATION`（作为拒单闸） |
| `other` | 如 `momentum`、`WEAK_TAPE`（报告但不作为 G4 缺项） |

```text
reject_rate[bucket] = count(bucket) / n_entry_evals
```

`auto_paper_orders=false` 的 skip **不**记入拒单（用户开关，不是市场不可执行）。G4 只要求三桶 **出现在响应里**（可全 0）；不设拒单率上限。高 `progress_band` 是预期。

### 2.4 扣协议费（用户选择 2026-09-22）

G3 的 **< 60** 用扣费中位，不用含费中位。期望、`sample_ok`（≥30 已平仓）、影子滑点、硬顶 80、`liveEnabled=false` **不变**。

```text
impact_gross_bps = estimated_impact_bps          # 现有入场冲击（含费地板）
impact_net_bps   = max(0, impact_gross_bps − protocol_fee_bps)
Go(G3)           = median(impact_net_bps) < 60
                 ∧ max(impact_gross_bps) ≤ 80

gates.median_entry_impact.ok 为 false **只有**两种：
  扣费中位 >= 60
  或 任一含费样本 > 80
等于 80 仍过。入场缓冲 max_impact_bps=75 不参与这道门。
样本条数不够是 coverage（总 verdict 仍 no-go、灯为灰），不是这道门的失败。
```

`protocol_fee_bps` 按阶段取模型里的费地板（`protocol_fee_bps_for_phase`），不另写魔法数：

| 阶段 | 常数 | 从哪来 |
|------|------|--------|
| `curve` / `graduating` | **62.5** | `CURVE_IMPACT_FEE_FLOOR_BPS = DEFAULT_IMPACT_FEE_BPS / 2`。`estimated_curve_impact_bps` 在储备行走之后加 `fee_bps/2`；默认 `fee_bps = 125`，所以地板是 **62.5**。覆盖 `fee_bps` 时地板改为该值的一半。 |
| `amm`（GRADMOCK 等已迁移） | **10** | `AMM_IMPACT_FEE_FLOOR_BPS = DEFAULT_LIQUIDITY_SPREAD_BPS / 2`。迁移后虚拟储备清零，冲击退回 CEX `spread_bps/2 + 40×(notional/adv)^0.6`。`LiquidityCtx.spread_bps` 默认 **20**，常数项是 **10 bps**。 |

不要把曲线上的 `DEFAULT_PROTOCOL_FEE_BPS = 100`（`sol_after_buy_fee` / `buy_tokens_out` 路径费）当成这个地板。那 100 bps 已经进了价格行走，含在 `impact_gross_bps` 的非线性部分里；G3 减去的是冲击公式末尾那截 **平坦费**。

纸面策略入场在 bonding curve，缺 `phase` 时按 `curve`（62.5）计。响应同时给含费与扣费，UI 标「含费 / 扣费」。

### 2.5 入场冲击样本与平仓笔数

G3 中位是 **一笔已平仓一条**。分母是 `PaperTradeJournal` 的 closed，不是 DecisionLog 里还留着的几条 fill。DecisionLog 只有 3 条入场 fill 时，不得把 `gates.median_entry_impact.n` 缩成 3（那样扣费中位即使约 17 bps、小于 60，也会因为 n 不足变成 `ok=false`）。

每笔纸面开仓在平仓 round-trip 上写入：

- `entry_estimated_impact_gross_bps`（含费；与 `entry_estimated_impact_bps` 相同）
- `entry_protocol_fee_bps`
- `entry_estimated_impact_net_bps` = max(0, gross − fee)

Fill 上对应 `estimated_impact_gross_bps` / `estimated_impact_net_bps` / `protocol_fee_bps`。只有含费 gross、尚未拆费的旧 lot，平仓时用阶段地板补上 fee 与 net。closed 缺字段时，只从 **已经记在** 开仓 fill 或 DecisionLog 上的冲击回填，不新造数字。

`gates.median_entry_impact.ok`（`basis=net_of_protocol_fee`）**只**在扣费中位 ≥ 60，或任一含费样本 > 80 时为 false。短样本记在 `coverage_ok`：总 `verdict` 仍要入场冲击条数 ≥ 30 才可能 go，灯为灰（证据不足），但这不是 60/80 失败。`sample_ok` 仍是 ≥30。`liveEnabled` 仍为 false。纸面入场在含费冲击 > 80 时直接拒单；默认 `max_impact_bps=75` 是硬顶下的缓冲。

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
median_entry_impact_bps                 # 含费中位（= gross；兼容旧字段）
median_entry_impact_gross_bps           # 含费
median_entry_impact_net_bps             # 扣费；G3 中位看这个 < 60
protocol_fee_bps                        # 本窗口实际扣的费中位
protocol_fee_bps_curve=62.5, protocol_fee_bps_amm=10
hard_max_impact_bps=80                  # 仍约束含费 max
impact_cap_go_bps=60                    # 约束扣费中位
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
| `max_notional_sol` | **1** | 纸面策略默认 0.12；实盘不得高于 1 |
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
- [x] 门：30 笔、期望≥0、**扣费**中位冲击&lt;60（含费硬顶 80）、三桶拒单率、影子滑点≤40bps、`liveEnabled=false`
- [x] 2026-09-22：G3 用 `impact_net_bps`（曲线费地板 62.5 = `DEFAULT_IMPACT_FEE_BPS/2`；AMM 10 = 默认 spread/2）
- [x] 2026-09-21：G3 按已平仓逐笔计 n；journal 写入 gross/fee/net；短 DecisionLog 不覆盖 journal
- [x] `GET /api/v1/stats/executability` 无密钥、无链上 send
- [x] 聚合器单测 + fixtures
- [x] 不打开 live、不放宽 LiveLimits、不改 `max_impact_bps` 硬顶 80

版本：v0。只追加门，不改已锁阈值，除非另开 RFC。  
2026-09-22 用户选择：G3 中位从含费改为扣协议费（&lt;60 不变，硬顶 80 仍看含费）。`liveEnabled` 默认仍 false。

---

## 7. Paper round 4 Go 窗（2026-09-21）

纸面 round 4 在下列 `PumpPaperParams` **进程默认**下得到 `verdict=go`。这是默认值，不是运行时 PUT。`liveEnabled` 仍为 **false**。`auto_paper_orders` 默认仍 **false**。实盘 `max_notional_sol` 硬顶仍是 **1.0 SOL**。含费硬拒仍是 **> 80**（等于 80 仍过）。Go 门不放宽：样本 ≥ 30、期望 ≥ 0、扣费中位 < 60、含费硬顶 80。

| 指标 | round 4 |
|------|---------|
| `verdict` | **go** |
| `n_closed` | **30** |
| expectancy | **≈ +0.00123** |
| median net impact | **≈ 12.4 bps** |
| max gross impact | **≈ 75.7 bps** |
| exit mix | **TP 16 / MAX_HOLD 12 / STOP_LOSS 1** |

| 参数 | 纸面默认 |
|------|----------|
| `take_profit_pct` | **0.10** |
| `stop_loss_pct` | **0.07** |
| `max_hold_sec` | **300** |
| `progress_bps_min` | **1200** |
| `progress_bps_max` | **6500** |
| `max_impact_bps` | **75**（硬拒仍是含费 > 80） |
| `max_notional_sol` | **0.12** |

习惯分桶（`0_800` … `7500_9000`、`pct_entries_800_7500`）仍是观察标签，不随这组入场窗改写。

---

## 8. 纸面强 tape 入场（round 4 之后）

Round 4 Go 窗在 `n=30` 时期望 ≈ **+0.00123**。同一组默认扩到 `n=51` 后期望 ≈ **−0.0003**，`verdict` 从 go 漂到 no-go。冲击仍过门。本轮只收紧纸面买入 tape，**不**改 G1–G6，**不**放宽含费硬顶 80 / 默认缓冲 75，**不**把 `liveEnabled` 或 `auto_paper_orders` 默认打开。

现有 tape 没有单独的买卖笔数。`aggregate_tape` 的 60s 窗口已有 `trade_count_1m`、`buy_notional_1m`、`sell_notional_1m`。粗动能仍先拒（`momentum`：买名义 ≥ 2× 卖名义且笔数 ≥ 8）。过了粗动能、但不够强的纸面开仓再拒，原因码 **`WEAK_TAPE`**：

| 参数 | 默认 |
|------|------|
| `min_trade_count_1m` | **10**（`trade_count_1m`） |
| `min_buy_sell_notional_ratio` | **2.5**（`buy_notional_1m ≥ 2.5 × sell_notional_1m`） |

出场不走这道门。`WEAK_TAPE` 落在拒单桶 `other`，不充当 G4 的 progress / impact / risk 缺项。卖盘为 0 时，只有买名义 > 0（或把比率阈值设成 0）才算过比率。
