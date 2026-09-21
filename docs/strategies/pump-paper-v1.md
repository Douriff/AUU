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
2. `800 <= progress_bps <= 7500`（过早噪音大，过晚拥挤）
3. `buy_notional_1m >= 2 * sell_notional_1m` 且 `trade_count_1m >= 8`
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
| 浮盈 `>= take_profit_pct`（默认 **14%**） | 全平 |
| 浮亏 `<= -stop_loss_pct`（默认 **9%**） | 全平 |
| `progress_bps >= 9000` 或 `complete` | 全平（毕业拥挤） |
| `sell_notional_1m >= 2 * buy_notional_1m` 持续 30s | 全平 |
| `estimated_impact_bps` 对平仓侧 `> 250` | 分两笔减仓（纸面） |
| 持仓超过 `max_hold_sec`（默认 **420**） | 全平 |

### 4.1 脱离监控列表的持仓（orphan）

`PumpPaperEngine.tick` 每拍评估的标的是 `provider.list_symbols()` **与** 当前纸面 `positions` 的并集。发现/自选把 mint 移出 watch list 后，持仓仍跑 `evaluate`（止盈 / 止损 / `max_hold_sec`）。

没有实时 snapshot 时：优先用上一笔曲线缓存；没有缓存则用 `position.entry_price` 合成最小纸面标记（`synthetic=true`，初始 virtual reserves，不是链上报价）。若该 orphan 已超过 `max_hold_sec` 而常规纸面平仓没有成交，再强制一笔纸面卖出，原因 `max_hold`，标签 `ORPHAN_EXIT`。这样 `max_open_mints` 不会被掉出列表的仓位永久占满。

本路径只走 PaperBroker。**不**发链上交易；`liveEnabled` 保持默认 **false**。

### 4.2 纸面出场默认（策略确认，2026-09-21）

30 笔已平仓里约 13 胜 / 17 负，期望约 −0.003。出场标签以 `MAX_HOLD`（约 19）为主，`STOP_LOSS` 约 6，`TAKE_PROFIT` 约 3：仓位经常在 900 秒时钟上结束，到不了 25% 止盈。

纸面默认写在 `PumpPaperParams`（进程启动即用）：

| 参数 | 值 | 作用 |
|------|----|------|
| `max_hold_sec` | **420** | 短于原先 900s 超时 |
| `take_profit_pct` | **0.14** | 止盈仍高于止损 9% |
| `stop_loss_pct` | **0.09** | 亏损先砍 |
| `max_notional_sol` | **0.12** | 压低曲线冲击 |
| `max_impact_bps` | **75** | 入场缓冲；硬顶仍是 **80** |

成交仍只来自 PaperBroker。`max_day_loss_pct=0.05`、`max_open_mints=3`、`notional_pct_equity=0.005`、`auto_paper_orders=false`。`liveEnabled` 仍为 **false**。Go 门槛不放宽：`sample_ok` ≥ 30、扣费中位 < 60、含费 **> 80** 才算硬顶失败（等于 80 仍过）。

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
progress_bps_min: 800
progress_bps_max: 7500
max_impact_bps: 75          # entry buffer; hard reject when gross impact > 80
take_profit_pct: 0.14
stop_loss_pct: 0.09
max_hold_sec: 420
cooldown_sec: 120
max_day_loss_pct: 0.05
max_open_mints: 3
notional_pct_equity: 0.005
max_notional_sol: 0.12
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

版本：v1。只加参数不改事件名。 硬顶仍是含费冲击 **80**（入场默认缓冲 `max_impact_bps=75`）。冻结：`progress_bps [800,7500]`，`notional_pct_equity 0.005`，`strategy_autopaper`/`auto_paper_orders` default false。`liveEnabled` 默认 false。

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
