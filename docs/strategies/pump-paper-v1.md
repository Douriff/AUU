# Pump.fun 纸面策略 v1（AUU）

状态：冻结草稿（2026-09-21）  
模式：**仅纸面 / 模拟**；默认 **不**自动下单。实盘需用户单独限额授权。  
对接：`SignalOut` · `RiskGate` · `PaperBroker` · `ctx.pump` · `DATA_PROVIDER=pumpfun_paper`

---

## 1. 目标

在 bonding curve 阶段捕捉「有持续买入动能、冲击可控、未过热毕业」的短线纸面机会；优先活下来（熔断）而非赌单笔暴击。

---

## 2. 监控盘面字段（策略输入）

| 字段 | 来源 | 用途 |
|------|------|------|
| `progress_bps` | `ctx.pump` | 曲线进度 0–10000 |
| `complete` / `migrated` | `ctx.pump` | 毕业/迁移门禁 |
| `virtual_*` / `real_*` reserves | `ctx.pump` | 价与冲击 |
| `estimated_impact_bps` | RiskGate 曲线公式 | 入场硬闸 |
| `buy_notional_1m` / `sell_notional_1m` | tape 聚合 | 动能 |
| `trade_count_1m` | tape | 活跃度 |
| `unique_buyers_5m`（可选） | tape 地址粗计 | 分散度 |
| `new_token` | 只读发现（PumpPortal / logs） | 入自选；**不**当作入场 |

发现：`PUMPFUN_DISCOVERY=pumpportal|logs|off`（无 `PUMPFUN_PORTAL_API_KEY` 时默认 **off**；有 key 默认 pumpportal）。Key 仅 env，永不入库。Portal WS **400/403** → health `discoveryReason=portal_auth_rejected`（坏 key vs IP 禁：`docs/adapters/pumpportal-discovery-v0.md`）。发现模块禁止交易。

盘面 UI：新币表 + Progress + tape + 风险标签 + 策略信号叠加（见 `docs/viz/market-monitor-v1.md`）。

---

## 3. 入场（开多纸面）

**全部满足才发 `SignalOut.side=long`：**

1. `not complete` 且 `not migrated`
2. `1200 <= progress_bps <= 6500`（过早噪音大，过晚拥挤）
3. `buy_notional_1m >= min_buy_sell_ratio_1m * sell_notional_1m` 且 `trade_count_1m >= min_trade_count_1m`（默认 **2.5** 与 **10**；不足则 `reason=momentum`）。字段是已有 60s tape：`buy_notional_1m` / `sell_notional_1m` / `trade_count_1m`。
4. 入场含费冲击 `estimated_impact_gross_bps` **≤ `max_impact_bps`**（默认 **75**，80 硬顶下的缓冲）。**> 80** 一律拒单（`GROSS_IMPACT_HARD`），即使把参数抬到 80 以上。等于 80 且参数允许时可以过。
5. 无标签：`HONEYPOT` / `TAX_HIGH` / `SPREAD_TOO_WIDE`（若有外部打标）
6. 冷却：同 mint `cooldown_sec` 默认 **120s** 内不再开仓

默认纸面名义：账户权益的 **0.5%**，且单笔绝对上限 `max_notional_sol`（默认 **0.12 SOL**）。较小名义把曲线冲击压在 75 bps 缓冲内。

发现（`new_token`）只入自选表，**不等于入场**；仍须过上述 progress / 动能 / 冲击门。

---

## 4. 出场

任一触发即 `flat` / 减仓：

| 条件 | 动作 |
|------|------|
| 浮盈 `>= take_profit_pct`（默认 **10%**） | 全平 |
| 浮亏 `<= -stop_loss_pct`（默认 **7%**） | 全平 |
| `progress_bps >= 9000` 或 `complete` | 全平（毕业拥挤） |
| `sell_notional_1m >= sell_pressure_ratio * buy_notional_1m` 持续 `sell_pressure_sec`（默认 **1.5×** 与 **12s**） | 全平 |
| `estimated_impact_bps` 对平仓侧 `> 250` | 分两笔减仓（纸面） |
| 持仓超过 `max_hold_sec`（默认 **300**） | 全平 |

### 4.1 脱离监控列表的持仓（orphan）

`PumpPaperEngine.tick` 每拍评估的标的是 `provider.list_symbols()` **与** 当前纸面 `positions` 的并集。发现/自选把 mint 移出 watch list 后，持仓仍跑 `evaluate`（止盈 / 止损 / `max_hold_sec`）。

没有实时 snapshot 时：优先用上一笔曲线缓存；没有缓存则用 `position.entry_price` 合成最小纸面标记（`synthetic=true`，初始 virtual reserves，不是链上报价）。若该 orphan 已超过 `max_hold_sec` 而常规纸面平仓没有成交，再强制一笔纸面卖出，原因 `max_hold`，标签 `ORPHAN_EXIT`。这样 `max_open_mints` 不会被掉出列表的仓位永久占满。

本路径只走 PaperBroker。**不**发链上交易；`liveEnabled` 保持默认 **false**。

发现列表满时，有纸面或实盘未平仓的 mint **不**走 `_drop_locked`（见 §4.4）。曲线和 1m tape 留在 provider 上，卖压仍读得到名义。只有快照真的没了，才用本节的 orphan 标记。

### 4.2 纸面 Go 窗默认（paper round 4，2026-09-21）

Paper round 4 在这组进程默认上得到 `verdict=go`：`n_closed=30`，expectancy ≈ +0.00123，扣费入场冲击中位 ≈ 12.4 bps，含费最大 ≈ 75.7 bps，出场 TP 16 / MAX_HOLD 12 / STOP_LOSS 1。

纸面默认写在 `PumpPaperParams`（进程启动即用）：

| 参数 | 值 | 作用 |
|------|----|------|
| `progress_bps_min` | **1200** | 收窄入场窗，避开过早噪音 |
| `progress_bps_max` | **6500** | 避开近毕业拥挤 |
| `max_hold_sec` | **300** | 短持仓；出场以止盈为主 |
| `take_profit_pct` | **0.10** | 止盈仍高于止损 7% |
| `stop_loss_pct` | **0.07** | 亏损先砍 |
| `max_notional_sol` | **0.12** | 压低曲线冲击 |
| `max_impact_bps` | **75** | 入场缓冲；硬顶仍是 **80**（含费 **> 80** 拒单） |

成交仍只来自 PaperBroker。`max_day_loss_pct=0.05`、`max_open_mints=3`、`notional_pct_equity=0.005`、`auto_paper_orders=false`。`liveEnabled` 仍为 **false**。实盘名义硬顶仍是 **1.0 SOL**。Go 门槛不放宽：`sample_ok` ≥ 30、扣费中位 < 60、含费 **> 80** 才算硬顶失败（等于 80 仍过）。

### 4.3 弱 tape 提前出场（paper round 6，2026-09-22）

Round 6 用 TP **0.08** / SL **0.06** / `max_hold_sec` **210** 跑到 `n=31` 时期望 E≈**+0.000034**，到 `n=35` 翻负。冲击门仍过。检查点出场构成是 **TP 6 / MAX_HOLD 24 / STOP_LOSS 1**。主问题是 **MAX_HOLD 占主导**：仓位常常要等到时间止损才走，已有的卖压规则（卖名义 ≥ 2× 买名义，且持续 30s）来得太晚。

本轮不改那次试验的止盈/止损/持仓默认（进程默认仍是 **0.10 / 0.07 / 300**），也不改入场动能 **10 / 2.5**。只把已经存在的卖压出场从写死常量收成 `PumpPaperParams`，并把默认提前到 **12s**、**1.5×**。Go 门与含费冲击硬顶 **80** / 缓冲 **75** 不改。`auto_paper_orders` 与 `liveEnabled` 默认仍是 **false**。

方向与旧代码一致，是卖侧更重，不是入场那条买/卖比：

```text
sell_notional_1m >= sell_pressure_ratio * max(buy_notional_1m, 1e-18)
  and that inequality has held for sell_pressure_sec
  → reason = sell_pressure, tag SELL_PRESSURE
```

买名义为 0 时仍用极小地板，纯卖出 tape 一样算卖压。计时从比值成立的第一拍开始；比值掉回去就清零，必须连续达到 `sell_pressure_sec`。

| 参数 | 默认 | 原先写死 |
|------|------|----------|
| `sell_pressure_sec` | **12** | 30 |
| `sell_pressure_ratio` | **1.5** | 2.0 |

`PUT` 与 `PATCH /api/v1/strategy/pump-paper-v1` 可改这两键，也可以设回 **30 / 2.0**。蒸馏 allowlist 不含它们，`apply-distill` 不会改卖压出场。入场仍只读 `min_trade_count_1m` 与 `min_buy_sell_ratio_1m`。

### 4.4 持仓不被发现淘汰清掉 tape（paper round 7，2026-09-22）

Round 7 纸面 **0** 笔 `sell_pressure`、**22/30** 是 `MAX_HOLD`。卖压参数和 `evaluate()` 已接通，但发现列表满时 `register_watch_mint` 对最老的 `discovered` mint 调用 `_drop_locked`，连 `_trades` 一起丢掉。持仓接着走 orphan 快照，`get_recent_trades` 为空，`buy_notional_1m` 与 `sell_notional_1m` 都是 0，`sell >= ratio * buy` 永不成立，计时不起步。

淘汰改为认仓（纸面 journal、实盘 journal、进程内策略持仓，按 symbol 或 mint）：

- 优先整段丢掉**无仓**的发现 mint（曲线和 tape 都删，行为与以前相同）。
- 名额全是持仓时，最老的一笔只清 `discovered`（让出发现名额），**保留**曲线和成交缓冲。之后 `tick()` / `evaluate()` 仍读到非空 tape，卖压可以触发。
- 不改 Go 门，不放宽入场动能默认 **10 / 2.5**，不把 `auto_paper_orders` 或 `liveEnabled` 默认打开。

---

## 5. 组合熔断（日级）

| 规则 | 默认 |
|------|------|
| 日亏达权益 `max_day_loss_pct` | **5%** → `trading_state=halted`，当日禁开 |
| 连续拒单/滑点拒单 | **5** 次 → 冷却 10min |
| 最大同时持仓 mint 数 | **3** |

---

## 6. 执行路径

```
Monitor tape/curve
  → Strategy.evaluate → SignalOut
  → RiskGate.pre_order（曲线冲击 + 熔断）
  → PaperBroker.submit
  → Fill / reject → Overlay + 告警
```

`auto_paper_orders` / `strategy_autopaper`：**默认 false**。仅当 Settings 打开且未 halt 时，信号才自动打纸面单。发现模块不发单。

---

## 7. 参数表（可配置）

```yaml
progress_bps_min: 1200
progress_bps_max: 6500
max_impact_bps: 75          # entry buffer; hard reject when gross impact > 80
take_profit_pct: 0.10
stop_loss_pct: 0.07
max_hold_sec: 300
cooldown_sec: 120
max_day_loss_pct: 0.05
max_open_mints: 3
notional_pct_equity: 0.005
max_notional_sol: 0.12
min_trade_count_1m: 10           # entry momentum; was hardcoded 8
min_buy_sell_ratio_1m: 2.5       # buy_notional_1m >= ratio * sell_notional_1m; was hardcoded 2.0
sell_pressure_sec: 12            # weakening-tape exit dwell; was hardcoded 30
sell_pressure_ratio: 1.5         # sell_notional_1m >= ratio * buy_notional_1m; was hardcoded 2.0
auto_paper_orders: false
strategy_autopaper: false   # alias of auto_paper_orders; default off
```

---

## 8. 验收（纸面）

- [ ] 盘面能列出仿真 mint 并刷新 progress / tape
- [ ] 满足入场条件时出现 long marker；拒单出现 risk/reject 无假 Fill
- [x] 打开 `auto_paper_orders` / `strategy_autopaper` 后才自动出纸面成交（health 双字段，无需重启）
- [x] 触发日亏熔断后无法再开仓
- [x] `new_token` 入自选但不绕过入场门；发现模块无下单
- [x] 纸面成功概率：`GET /api/v1/stats/paper-performance`（平仓样本；蒙特卡洛标明 simulation）
- [x] 持仓 mint 离开 `list_symbols` 后，超过 `max_hold_sec` 仍纸面平仓（orphan exit；`liveEnabled` 仍 false）
- [x] 持仓 mint 被发现列表淘汰时不丢 tape；卖压仍能在 `max_hold_sec` 之前触发（`liveEnabled` 仍 false）

版本：v1。只加参数不改事件名。 硬顶仍是含费冲击 **80**（入场默认缓冲 `max_impact_bps=75`）。纸面 Go 窗：`progress_bps [1200,6500]`，`take_profit_pct 0.10`，`stop_loss_pct 0.07`，`max_hold_sec 300`，`max_notional_sol 0.12`，`notional_pct_equity 0.005`。入场动能默认 `trade_count_1m ≥ 10` 且买名义 ≥ **2.5×** 卖名义（仍是 `momentum`）。卖压出场默认卖名义 ≥ **1.5×** 买名义并持续 **12s**（`sell_pressure`；原先写死 2.0× / 30s）。`strategy_autopaper`/`auto_paper_orders` default false。`liveEnabled` 默认 false。实盘名义硬顶 1.0 SOL 不变。

---

## 9. 观察蒸馏 overlay（非镜像钱包）

对齐 `docs/adapters/trader-watch-distill-v0.md`。蒸馏产出 **`PumpPaperParamsPatch` + feature_weights**，不是「抄他下一笔」。

- `POST /api/v1/watch/traders/{id}/distill` 只计算
- `POST /api/v1/strategy/pump-paper-v1/apply-distill` 须 `confirm=true` 才写入 params
- **默认不改** `auto_paper_orders` / `strategy_autopaper`
- `copy_trade_enabled=false` 硬编码；无 mirror 路径
- `sniper`：**不**降低 `progress_bps_min`（拒绝自动套用入场窗）
- `graduation_chase`：**不**抬高 `progress_bps_max`（与毕业熔断冲突，默认拒绝）
- `mid_curve`：向其 `entry_progress_median` 收紧入场窗（同族，优先采纳）
- `flip`：缩短 `max_hold_sec`，略降 `take_profit_pct`
- `bag`：放宽 `max_hold_sec`，收紧 `stop_loss_pct`（不取消日亏熔断）
- `feature_weights.impact` 更厌恶冲击时仍受 `max_impact_bps=80` 硬顶
- Journal / 成功概率仍只吃自有纸面；`CompareReport` 为 `reference_only`
