# 交易员观察 → 习惯蒸馏 → 自有策略 · 接口草图 v0

产品定稿草案（望舒）：观察 Pump 交易员 → 习惯/持仓总结 → **形成自有策略**。  
明确：**不是盲跟单**；不自动镜像钱包实盘；纸面优先；私钥不进仓。

对齐：冻结 `SignalOut` / `RiskGate` / `PaperBroker` / `PaperTradeJournal`；`pump-paper-v1` 因子（progress、动能、冲击）。

---

## 0. 原则

| 做 | 不做 |
|----|------|
| 只读观察地址的公开成交/持仓快照 | 自动复制每笔链上单 |
| 标签化习惯 → 调参/加权自有规则 | 把「跟单信号」直接当 `OrderIntent` |
| 纸面回放：用蒸馏参数跑 `pump-paper-v1`，Journal 对照 | live 默认开；无二次确认发真链 |

流水线：

```text
Watcher(readonly) → TraderSnapshot 流
                 → HabitEngine → HabitTags + HabitProfile
                 → Distiller → PumpPaperParamsPatch / FeatureWeights
                 → pump-paper-v1.evaluate（自有信号）
                 → 可选 autopaper（仍走 RiskGate→PaperBroker）
                 → PaperTradeJournal 对照（自有纸面 vs 观察对象参考曲线，仅展示）
```

---

## 1. `TraderWatchlist`

```text
TraderWatchlistItem:
  watch_id: str
  address: str                 # Solana 钱包（观察对象）
  label: str | null            # 用户备注
  enabled: bool                # 默认 true
  source: portal|rpc|indexer   # 只读发现/成交源（开源调研定）
  added_ts: int
  tags_override: list[str]     # 用户手动钉标签（可选）
  risk_notes: str | null       # 如「疑似 bot」人工标记
```

API（草）：
- `GET/PUT /api/v1/watch/traders` — 列表增删改
- 无私钥字段；地址仅公开观测

---

## 2. `TraderSnapshot`（按时点 / 滚动窗）

由只读成交+持仓聚合，带 `asof_ts`：

```text
TraderSnapshot:
  watch_id / address: str
  asof_ts: int                 # ms
  slot: int | null

  # 持仓
  positions: list[{
    mint, symbol?,
    qty, cost_basis_sol?,
    unrealized_pnl_sol?,
    hold_sec,                  # 该仓已持时长
    progress_bps,              # 该 mint 当前曲线进度（若仍在 curve）
    phase: curve|graduating|amm|unknown
  }]
  open_count: int
  gross_exposure_sol: float

  # 近窗行为（默认 T=1h / 24h 两档）
  recent_buys: list[TradeBrief]   # 近买
  recent_sells: list[TradeBrief]  # 近卖
  buy_notional_1h / sell_notional_1h: float
  trade_count_1h: int
  median_hold_sec_24h: float | null
  flip_rate_24h: float | null     # 短持平仓笔数 / 总平仓

  # 曲线进度分布（持仓+近买 mint）
  progress_hist: {             # bps 桶
    "0_800": int,
    "800_5000": int,
    "5000_7500": int,
    "7500_9000": int,
    "9000_10000": int,
    "migrated": int
  }
  entry_progress_median_bps: int | null   # 近买入时 progress 中位数

TradeBrief:
  ts, mint, side: buy|sell, sol_amount, progress_bps?, signature?
```

WS（可选）：`type:"trader_snapshot"`，payload=`TraderSnapshot`  
与盘面 `new_token` 分立；不进 Overlay 成交点。

---

## 3. `HabitTags`（习惯标签）

多标签可并存，带置信度与证据：

```text
HabitTag:
  tag: sniper | mid_curve | graduation_chase | flip | bag
  confidence: float ∈ [0,1]
  evidence: list[str]          # 人可读短句 / 特征键
  # 同义映射（写入前规范化）：curve_mid→mid_curve，quick_flip→flip
  # 完整表见 docs/research/trader-learning-datasources.md §2

HabitProfile:
  watch_id / address
  asof_ts
  tags: list[HabitTag]
  primary: HabitTag | null     # confidence 最高
  features: {                  # 蒸馏用原始特征
    median_entry_progress_bps,
    pct_entries_lt_800,        # sniper 倾向
    pct_entries_800_7500,      # mid_curve
    pct_entries_gt_9000,       # graduation_chase
    median_hold_sec,
    flip_rate_24h,
    bag_score,                 # 长持+未实现占比
  }
```

**判定草图（可调参，非盲跟）：**

| tag | 启发式（默认） |
|-----|----------------|
| `sniper` | `pct_entries_lt_800 ≥ 0.5` 且 `median_hold_sec` 偏短 |
| `mid_curve` | `pct_entries_800_7500 ≥ 0.45`（观察桶 800–7500；纸面入场窗是 `[1200, 6500]`） |
| `graduation_chase` | `pct_entries_gt_9000 ≥ 0.35` 或持仓 progress 中位数 ≥ 9000 |
| `flip` | `flip_rate_24h ≥ 0.5` 且 `median_hold_sec < 300` |
| `bag` | `median_hold_sec > 3600` 或大仓长期未平 |

输出 API：`GET /api/v1/watch/traders/{id}/habits`

---

## 4. 蒸馏进 `pump-paper-v1`（自有策略，非镜像）

蒸馏器产出 **参数补丁 + 特征权重**，不产出「抄他下一笔」：

```text
DistillResult:
  source_watch_id: str
  asof_ts: int
  suggested_params: PumpPaperParamsPatch   # 只含允许键
  feature_weights: {
    progress: float,
    momentum: float,
    impact: float
  }
  enabled_tags: list[str]                  # 用户确认后才应用
  reject_reason: str | null                # 如 sniper 主导 → 默认不建议套用入场窗
  paper_compare: {                         # 可选：同区间自有纸面 vs 观察权益（展示）
    self_stats_ref,
    trader_ref_curve_id
  }
```

**标签 → 参数映射（建议默认，需用户点「应用蒸馏」）：**

| 主导习惯 | 对 `PumpPaperParams` 的建议 | 说明 |
|----------|-----------------------------|------|
| `mid_curve` | 保持/收紧 `progress_bps_min/max` 向其 `entry_progress_median` | 与现策略同族，优先采纳 |
| `graduation_chase` | **不**自动抬高 `progress_bps_max`；仅提示风险；或单独 `watch_only` | 与现熔断冲突，默认拒绝自动套用 |
| `sniper` | 不降低 `progress_bps_min`；可只用于 Watchlist 警示 | 避免把策略改成抢跑 |
| `flip` | 缩短 `max_hold_sec`，略降 `take_profit_pct` | 快进快出，仍过冲击闸 |
| `bag` | 放宽 `max_hold_sec`，收紧 `stop_loss_pct` | 长持，但不取消日亏熔断 |

动能/冲击：
- `feature_weights.momentum` ↑ 若观察对象高 `buy_notional` 失衡稳定  
- `feature_weights.impact` ↑（更厌恶冲击）若其常大单；↓ 仅作研究，**实盘/纸面仍受 `max_impact_bps=80` 硬顶**

应用 API：
- `POST /api/v1/watch/traders/{id}/distill` → `DistillResult`（只计算）
- `POST /api/v1/strategy/pump-paper-v1/apply-distill` → 需 `confirm=true`；写入 params；**默认不改 `auto_paper_orders`**

---

## 5. 与 Journal 对照（不是跟单归因）

```text
CompareReport:
  window: {from_ts, to_ts}
  self: PaperStats              # 现有 journal，source=paper
  trader_ref: {
    # 观察地址同窗「假想权益」：仅用其公开成交估值，标记 reference_only
    n_trades, approx_pnl, tags_hist
  }
  note: "reference_only — not copy-trading"
```

面板：成功概率旁可挂「对照」折叠；**胜率仍只吃自有纸面 Journal**。

---

## 6. 风控 / live 边界

- 观察与蒸馏 **永不** 触发 live 发送  
- `liveEnabled` / `LiveLimits` 与本模块无关  
- 若未来「一键纸面按蒸馏参数跑」，仍：`Signal → RiskGate → PaperBroker`  
- 拒单原因码不新增跟单类；习惯警示可用 `debug` / 独立 `habit_alert` WS

---

## 7. 实现顺序（建议）

1. Watchlist CRUD + 空 Snapshot 桩（mock 成交）  
2. HabitEngine 规则 + tags API  
3. Distill → params patch（默认不自动 apply）  
4. 开源调研数据源接通只读成交  
5. CompareReport 展示  

---

## 8. 版本

- **v0**：接口与映射冻结字段名；算法阈值可调  
- 不自动镜像钱包；产品文案禁止「跟单/复制交易」字样，用「观察 / 蒸馏 / 自有策略」
