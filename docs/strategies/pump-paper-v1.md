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

发现：`PUMPFUN_DISCOVERY=pumpportal|logs|off`（无 `PUMPFUN_PORTAL_API_KEY` 时默认 **off**；有 key 默认 pumpportal）。Key 仅 env，永不入库。发现模块禁止交易。

盘面 UI：新币表 + Progress + tape + 风险标签 + 策略信号叠加（见 `docs/viz/market-monitor-v1.md`）。

---

## 3. 入场（开多纸面）

**全部满足才发 `SignalOut.side=long`：**

1. `not complete` 且 `not migrated`
2. `800 <= progress_bps <= 7500`（过早噪音大，过晚拥挤）
3. `buy_notional_1m >= 2 * sell_notional_1m` 且 `trade_count_1m >= 8`
4. `estimated_impact_bps(order_notional) <= max_impact_bps`（默认 **80**）
5. 无标签：`HONEYPOT` / `TAX_HIGH` / `SPREAD_TOO_WIDE`（若有外部打标）
6. 冷却：同 mint `cooldown_sec` 默认 **120s** 内不再开仓

默认纸面名义：账户权益的 **0.5%**，且单笔绝对上限 `max_notional_sol`（默认仿真 **0.5 SOL 等值**）。

发现（`new_token`）只入自选表，**不等于入场**；仍须过上述 progress / 动能 / 冲击门。

---

## 4. 出场

任一触发即 `flat` / 减仓：

| 条件 | 动作 |
|------|------|
| 浮盈 `>= take_profit_pct`（默认 **25%**） | 全平 |
| 浮亏 `<= -stop_loss_pct`（默认 **12%**） | 全平 |
| `progress_bps >= 9000` 或 `complete` | 全平（毕业拥挤） |
| `sell_notional_1m >= 2 * buy_notional_1m` 持续 30s | 全平 |
| `estimated_impact_bps` 对平仓侧 `> 250` | 分两笔减仓（纸面） |
| 持仓超过 `max_hold_sec`（默认 **900**） | 全平 |

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
max_impact_bps: 80
take_profit_pct: 0.25
stop_loss_pct: 0.12
max_hold_sec: 900
cooldown_sec: 120
max_day_loss_pct: 0.05
max_open_mints: 3
notional_pct_equity: 0.005
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

版本：v1。只加参数不改事件名。 Frozen params（2026-09-21）：`progress_bps [800,7500]`，`max_impact_bps 80`，`notional_pct_equity 0.005`，`strategy_autopaper`/`auto_paper_orders` default false。
