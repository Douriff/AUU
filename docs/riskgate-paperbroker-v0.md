# RiskGate + PaperBroker 适配草图 v0

对齐：冻结字段 `StrategyDecision` / `RiskOut` / `Fill`；开源映射 Nautilus→RiskGate，Hummingbot→Executor 节奏（本稿不含 Executor）。  
模式：仅纸面/模拟。

---

## A. RiskGate 适配草图

### A.1 职责

`RiskGate.check(ctx, signal, size) -> RiskOut`

- 输入：`StrategyContext` + `SignalOut` + `SizeOut`
- 输出：`RiskOut{allow, clipped_size, tags, notes}`
- 钩子顺序（与接口草案一致）：在流水线中位于 Sizer 之后、Executor 之前；内部再跑 `post_size` 复核 + `pre_order` 门禁。

### A.2 开源映射列（已冻结 · 2026-09-20）

依据：`/workspace/deepdive-channel-brief.md`、`jesse-strategy-deepdive.md`、`nautilus-riskengine-mapping.md`。  
原则：只借检查清单与节奏；**不**嵌 Nautilus 整引擎；Jesse **不**接全局 store / 真仓。

#### A.2.1 Nautilus → RiskGate / PaperBroker

| 我们的钩子 | Nautilus 概念 / 路径 | 本平台行为 | 借/不借 |
|------------|----------------------|------------|---------|
| `pre_order` | Submit 全套：`check_order*` / notional / balance / reduce_only / GTD / 速率 | 接单前硬闸；deny → 无 Fill，告警吃原因码 | ✅ 清单内化 |
| `pre_order` + kill switch | `TradingState` Active / Reducing / Halted | Halted 拒一切新开；Reducing 仅减仓 | ✅ |
| `pre_order` 响应 | `OrderDenied` / `OrderModifyRejected` | 标准化 tags → SQLite/WS | ✅ |
| — | `RiskEngine.process`（多仅日志） | **不当** post_fill | ❌ 弱相关 |
| `post_fill` | ExecutionEngine overfill / duplicate | 成交完整性；拒异常 fill | ✅ |
| `post_fill` | Fill 后组合/日亏 → Reducing/Halted | 自研熔断状态机 | ✅ |
| Sizer（非本钩子） | `FixedRiskSizer` | → `StrategyDecision.size` | ✅ 思想 |
| PaperBroker | `FillModel`（prob_fill / slippage / 分层簿 / seed） | 限价概率、bps 滑点、可复现 seed；勿搬 MatchingEngine | ✅ 思想 |
| — | 整引擎嵌核 / 改 `crates/risk` | LGPL：动态依赖、检查点自研 | ❌ |

#### A.2.2 Jesse → Signal / Sizer / 旁路（填全口子）

| Jesse | 我们冻结字段 | 借/不借 |
|-------|--------------|---------|
| `should_long` / `should_short` | `signal`（`long\|short\|flat` + strength） | ✅ |
| `go_long` / `go_short` 组装 qty/price | `intent`（无副作用下单） | ✅ |
| `risk_to_qty` / `size_to_qty` | `size`（Sizer） | ✅ 公式；fee 系数自研 |
| `filters` / `before`/`after` | 软闸 + `debug` | ✅；硬闸仍归 RiskGate |
| 仓位 `on_*_position` | 启发 `post_fill` 记账 | ✅ 节奏 |
| `jesse.research.backtest` | 研究入口纯函数 | ✅ |
| 全局 store / live sleep / 真仓 / `self.buy=` 副作用 | — | ❌ |

> **冻结声明：** A.2 映射列以此表为准；后续只追加行，不改钩子归属。LGPL SaaS 义务标「待核实」。

### A.3 伪代码

```python
# risk_gate.py — 纸面默认

from dataclasses import dataclass, field
from typing import Optional

REASON = {
    "DAY_LOSS_BREAKER", "SPREAD_TOO_WIDE", "DEPTH_THIN", "SLIPPAGE_CAP",
    "MEV_SUSPECT", "HONEYPOT_FLAG", "TAX_HIGH", "POSITION_CAP", "COOLDOWN",
}

@dataclass
class RiskOut:
    allow: bool
    clipped_size: Optional[object] = None  # SizeOut
    tags: list[str] = field(default_factory=list)
    notes: str = ""

class RiskGate:
    def __init__(self, limits, meme: dict):
        self.limits = limits
        self.meme = meme  # MemeRiskParams
        self._cooldown_until: dict[str, int] = {}

    def check(self, ctx, signal, size) -> RiskOut:
        tags: list[str] = []
        notes: list[str] = []
        s = size

        # --- post_size ---
        max_nom = self.limits.max_notional_per_symbol
        if abs(s.target_notional) > max_nom:
            s = replace_notional(s, sign(s.target_notional) * max_nom)
            tags.append("POSITION_CAP")
            notes.append(f"clip notional to {max_nom}")

        # 波动缩放
        rv = ctx.features.get("realized_vol_daily")
        tv = self.meme.get("target_vol_daily", 0.1)
        if rv and rv > 0:
            scale = min(1.0, tv / rv)
            if scale < 1.0:
                s = replace_notional(s, s.target_notional * scale)
                notes.append(f"vol_scale={scale:.3f}")

        # 单币权益占比
        eq = max(ctx.account.equity, 1e-9)
        if abs(s.target_notional) / eq > self.meme.get("max_single_symbol_pct", 0.1):
            cap = eq * self.meme["max_single_symbol_pct"]
            s = replace_notional(s, sign(s.target_notional) * cap)
            tags.append("POSITION_CAP")

        # --- pre_order ---
        if ctx.account.day_pnl / eq <= -self.limits.max_day_loss_pct:
            return RiskOut(False, None, ["DAY_LOSS_BREAKER"], "day loss breaker")

        if ctx.liquidity.spread_bps > self.limits.max_spread_bps:
            return RiskOut(False, None, ["SPREAD_TOO_WIDE"], "spread")

        if ctx.liquidity.adv_usd < self.meme.get("min_adv_usd", 5e4):
            return RiskOut(False, None, ["DEPTH_THIN"], "adv")

        impact = ctx.liquidity.estimated_impact_bps(abs(s.target_notional))
        if impact > self.meme.get("impact_cap_bps", 150) or impact > s.max_slippage_bps:
            return RiskOut(False, None, ["SLIPPAGE_CAP"], f"impact={impact}")

        if self.meme.get("honeypot_block") and ctx.meta.get("honeypot"):
            return RiskOut(False, None, ["HONEYPOT_FLAG"], "honeypot")

        tax = ctx.meta.get("tax_pct")
        if tax is not None and tax > self.meme.get("max_tax_pct", 10):
            return RiskOut(False, None, ["TAX_HIGH"], f"tax={tax}")

        until = self._cooldown_until.get(ctx.symbol, 0)
        if ctx.ts < until:
            return RiskOut(False, None, ["COOLDOWN"], "entry cooldown")

        if self.meme.get("mev_guard") and s.urgency == "high" and abs(s.target_notional) > eq * 0.05:
            s = replace_urgency(s, "normal")
            tags.append("MEV_SUSPECT")
            notes.append("downgrade urgency")

        if self.meme.get("rug_velocity_guard") and ctx.meta.get("rug_velocity"):
            if signal.side != "flat" and not is_reducing(ctx.position, s):
                return RiskOut(False, None, ["DEPTH_THIN"], "rug velocity: flat-only")

        return RiskOut(True, s, tags, "; ".join(notes))

    def on_fill(self, ctx, fill) -> None:
        """post_fill：记录拒绝冷却等。"""
        pass

    def on_reject(self, ctx, tags: list[str]) -> None:
        cd = self.limits.cooldown_sec_after_reject
        self._cooldown_until[ctx.symbol] = ctx.ts + cd * 1000
```

### A.4 给可视化 / 告警

- `allow=false` → 告警条展示 `tags`（原因码）+ `notes`
- `allow=true` 但 `tags` 非空 → 弱告警（已削减/降 urgency）

---

## B. PaperBroker 适配草图

### B.1 职责

`PaperBroker.submit(ctx, intent: OrderIntent) -> list[Fill] | Reject`

- 消费 `OrderIntent`（Executor 产出；暂无 Hummingbot 节奏时也可由薄封装直接传）
- 产出冻结字段 `Fill{ts, price, qty, fee, slippage_bps, tag}`
- 成交后回调 `RiskGate.on_fill`

### B.1.1 FillModel 借鉴（Nautilus，已冻结）

- 借：`prob_fill_on_limit`、`prob_slippage`（改 bps）、分层冲击、`random_seed` 可复现
- 不借：整 MatchingEngine；模因/AMM 曲线成交规则标「待核实」后单开
- overfill/duplicate 检测落在 RiskGate.`post_fill`，不在撮合层吞掉

### B.2 成交算法（偏保守）

```python
# paper_broker.py

@dataclass
class Fill:
    ts: int
    price: float
    qty: float
    fee: float
    slippage_bps: float
    tag: str

class PaperBroker:
    def __init__(self, fee_bps=15.0, base_bps=15.0, k=40.0, alpha=0.6, latency_ms=300):
        self.fee_bps = fee_bps
        self.base_bps = base_bps
        self.k = k
        self.alpha = alpha
        self.latency_ms = latency_ms
        self.open_orders: list = []

    def submit(self, ctx, intent) -> list[Fill]:
        # 延迟：成交时间戳推后；价格用「当前可观测」ctx（回测引擎负责不喂未来）
        fill_ts = ctx.ts + self.latency_ms

        if intent.order_type == "market":
            return self._fill_market(ctx, intent, fill_ts)
        if intent.order_type == "limit":
            return self._try_limit(ctx, intent, fill_ts)
        if intent.order_type == "twap_sim":
            return self._twap_slices(ctx, intent, fill_ts)
        return []

    def _slippage_bps(self, notional: float, adv_usd: float) -> float:
        adv = max(adv_usd, 1.0)
        return self.base_bps + self.k * ((notional / adv) ** self.alpha)

    def _fill_market(self, ctx, intent, fill_ts) -> list[Fill]:
        notional = abs(intent.qty_or_notional)
        # 有深度：按档吃；无深度：公式滑点
        if ctx.book:
            fills = walk_book(ctx.book, intent.side, notional)
        else:
            mid = ctx.tick.mid
            slip = self._slippage_bps(notional, ctx.liquidity.adv_usd)
            if slip > intent.max_slippage_bps:
                self._reject(ctx, "SLIPPAGE_CAP")
                return []
            px = mid * (1 + slip / 1e4) if intent.side == "buy" else mid * (1 - slip / 1e4)
            qty = notional / px
            fills = [(px, qty, slip)]

        out = []
        for px, qty, slip in fills:
            fee = abs(px * qty) * self.fee_bps / 1e4
            out.append(Fill(fill_ts, px, signed_qty(intent.side, qty), fee, slip, intent.client_tag))
        return out

    def _try_limit(self, ctx, intent, fill_ts) -> list[Fill]:
        # 仅当对侧最优穿越限价；否则挂入 open_orders，由 on_tick 撮合
        if not crossed(ctx, intent):
            self.open_orders.append(intent)
            return []
        # 成交价=限价（保守：不给更好价格）
        px = intent.limit_price
        qty = abs(intent.qty_or_notional) / px
        fee = abs(px * qty) * self.fee_bps / 1e4
        return [Fill(fill_ts, px, signed_qty(intent.side, qty), fee, 0.0, intent.client_tag)]

    def on_tick(self, ctx) -> list[Fill]:
        """回测引擎每个 tick/bar 调用，撮合挂单。"""
        done, rest = [], []
        for o in self.open_orders:
            if crossed(ctx, o):
                done.extend(self._try_limit(ctx, o, ctx.ts))
            else:
                if o.expire_ts and ctx.ts >= o.expire_ts:
                    continue  # 撤单
                rest.append(o)
        self.open_orders = rest
        return done
```

### B.3 与可视化约定

- 每个 `Fill` → K 线成交点（买/卖颜色分侧）
- `tag` = `OrderIntent.client_tag`（可带 strategy_id）
- `REJECT` 不进 Fill，走告警（可另发 `RejectEvent{ts, tags}`，v0.1 再加字段）

### B.4 回测引擎最小接线

```text
for event in market_events:
    ctx = build_context(event, portfolio)
    decision = strategy.on_context(ctx)   # 内含 Signal→Sizer→RiskGate→Executor
    if decision.intent and decision.risk.allow:
        fills = paper.submit(ctx, decision.intent)
        for f in fills:
            portfolio.apply(f)
            risk_gate.on_fill(ctx, f)
            emit(f)  # → 可视化 / SQLite
    fills2 = paper.on_tick(ctx)
    ...
```

---

## C. 下一步

1. ~~填 A.2~~ **已冻结**（本文件 A.2）。
2. 可选：Jesse → Signal/Sizer 薄适配伪代码单列一页（研究 API `/v1/strategy/decide`）。
3. 可视化：按已冻结 `Fill` / `RiskOut.tags` 接 Mock；等 Origin。
4. 不实现实盘适配器；不读写交易所密钥。
