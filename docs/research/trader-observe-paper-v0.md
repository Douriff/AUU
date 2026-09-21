# AUU 公开交易者观察 → 策略因子（纸面）v0

状态：调研规格（2026-09-21 Asia/Shanghai）  
范围：**只读公开数据 + 纸面信号**；禁止实盘、私钥、默认盲跟单。  
对齐：`docs/strategies/pump-paper-v1.md`、`docs/architecture.md`、`docs/contracts.md`。  
**数据源拍板（望舒）：** `docs/research/trader-learning-datasources.md` + `docs/research/trader-learning-channel-brief.md`。P0 离开 mock = 用户 Watchlist + Helius/RPC parsed Pump ix + `ctx.pump`；禁止刮 frontend-api / Photon / BullX / GMGN。

---

## 0. 产品澄清：Pump.com vs Pump.fun

| 名称 | 结论 |
|------|------|
| **pump.fun** | Solana bonding-curve meme launchpad（AUU 目标 venue）。官方程序文档/IDL：`pump-fun/pump-public-docs`；程序 `6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P`；毕业 AMM PumpSwap `pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA`。 |
| **pump.com** | HTTP 302 → `telepathy.com`（无关站）。**不是** Pump.fun。用户口中的 “Pump.com” 应按 **Pump.fun** 理解。 |
| **GO / go.fun（Pump 品牌）** | 赏金/社交类产品；条款明确禁止 scraper / data mining / 训练数据集。AUU **勿**爬 GO。 |

**官方数据 API：无。** Bitquery 等明确写：Pump.fun 只公开程序文档与 IDL；站点前端 host（`frontend-api-v3.pump.fun`、`profile-api.pump.fun` 等）**无公开 schema、无稳定 SLA、无官方授权**。社区逆向仓（BankkRoll/meefs）属 unofficial，仅作“存在哪些路径”的研究提示，**不作为 AUU 生产依赖**。

---

## 1. 今日可公开获得的数据（按合规优先）

### 1.1 推荐（合规友好 / 有文档）

| 来源 | 能拿到什么 | Auth / 限额 | AUU 用法 |
|------|------------|-------------|----------|
| **链上 + Helius** | 钱包 txs（含 ATA）、Parsed Events（buy/sell/create）、transfers、mint 持仓 | API key（env only）；按 plan RPS | **P0 真相源**：用户自选钱包 → 解码 Pump ix → 持仓/进出场/持仓时长 |
| **官方 IDL + `@pump-fun/pump-sdk`（MIT）** | 曲线账户、进度、费用、报价数学 | 无；RPC 读 | 已有 `pumpfun_paper`；快照 `progress_bps` / reserves |
| **Solana Tracker Data API** | `/trades/{token}`、`/top-traders/{token}`、`/first-buyers`、`/v2/pnl/wallets/{wallet}`、leaderboard、holders、graduated | `x-api-key`；Free 2.5k/月 · 3 rps；WS 需 Premium | 快速原型 PnL/榜单；**标注第三方** |
| **Bitquery GraphQL** | 实时/历史 trades、top traders、holders、creator fees（`collect_creator_fee`）、bonding progress、graduation | Access token；trial 后按 points；~30–240 req/min | 研究级历史与聚合；勿当唯一实时源 |
| **Birdeye** | `GET /defi/v2/tokens/top_traders`（volume/PnL/tags：sniper/smart_trader） | API key | 代币维度 top trader 标签 |
| **PumpPortal Data WS**（第三方） | `subscribeAccountTrade` / `subscribeTokenTrade` / `subscribeNewToken` | 部分免费；交易 API 另计费 | 已有 discovery；可扩 **watchlist 钱包成交推送**（只读） |
| **Dune** | 社区 Pump 仪表盘 / SQL | 免费查询额度有限 | 离线研究；人工导出 CSV → AUU 因子校准 |
| **PumpFunData parquet** | 按小时 swap parquet（`user_wallet`, buy/sell, lamports…） | API key；30 rpm；按 credit | 回测语料；非实时 |

### 1.2 慎用 / 默认禁用

| 来源 | 风险 |
|------|------|
| `frontend-api-v3.pump.fun` / `profile-api.pump.fun` | **Unofficial**；JWT + Origin 伪造；无 rate-limit 文档；ToS/脆变风险高 |
| Photon / BullX / GMGN **页面刮取** | Cookie、反爬、ToS；社区 scraper 依赖 `_photon_ta` 等 cookie → **禁止进 AUU** |
| GMGN / BullX OpenAPI | 若有正式 key 与 ToS 可议；无 key 的反代/刮取 → 禁用 |
| 任何会 `sendTransaction` / 带费交易 API | 纸面产品线外；live adapter 保持 default off |

### 1.3 创作者费用 / 排行榜

- Creator fees：链上 `collect_creator_fee`（Bitquery 有示例）或解码 fee program；非必须 P0。
- Leaderboards：Solana Tracker PnL V2 / Birdeye top_traders / Bitquery 按 mint 聚合；**不是**官方 Pump “全球排行榜 API”。

---

## 2. AUU 模块名与接口草图（纸面）

```text
TraderWatchlist  →  TraderSnapshotter  →  HabitTagger  →  FactorDistiller
                                                              ↓
                                              StrategyFactorBank (paper signals only)
                                                              ↓
                                         pump-paper-v1 params / SignalOut.tags
                                                              ↓
                                              RiskGate → PaperBroker（不变）
```

### 2.1 `TraderWatchlist`

- 用户**主动添加**的 Solana 地址列表（JSON/DB）；禁止默认全网刮榜入库。
- API 草图：
  - `GET/POST /api/v1/traders/watchlist` `{ wallet, label?, source:"user"|"imported_csv", enabled }`
  - `DELETE /api/v1/traders/watchlist/{wallet}`
- Env：`TRADER_WATCH_WALLETS=`（逗号分隔，仅开发）；**无私钥**。

### 2.2 `TraderSnapshot`（周期快照，如 60s）

```ts
interface TraderSnapshot {
  wallet: string;
  ts: number;                 // unix ms
  holdings: Array<{
    mint: string;
    qty: string;
    phase: "curve" | "amm" | "unknown";
    progress_bps?: number;    // 若仍在曲线
    notional_sol_est?: number;
  }>;
  recent_fills: Array<{       // 近 N 笔 Pump/PumpSwap
    mint: string;
    side: "buy" | "sell";
    sol_amount: number;
    ts: number;
    signature: string;
    progress_bps_at_fill?: number;
  }>;
  proxies: {
    median_hold_sec?: number;
    win_rate_closed?: number; // 已平仓粗 proxy，非会计 PnL
    avg_entry_progress_bps?: number;
    buy_size_p50_sol?: number;
    flip_ratio_15m?: number;  // 15m 内买卖回合占比
  };
  source: "helius" | "rpc" | "solanatracker" | "bitquery" | "pumpportal";
}
```

### 2.3 `HabitTag`（枚举，可多标签）

| Tag | 启发式（示例阈值，可配置） |
|-----|---------------------------|
| `sniper` | 买入 slot − create slot ≤ 2 **或** `progress_bps_at_fill < 300` |
| `curve_mid` | 买入 `progress_bps ∈ [800, 7500]`（观察桶；纸面 Go 窗是 `[1200, 6500]`） |
| `graduation_chase` | 买入 `progress_bps ≥ 8500` 或临近 `complete` |
| `quick_flip` | `median_hold_sec < 180` 且 `flip_ratio_15m` 高 |
| `bag_holder` | `median_hold_sec > 3600` 或未平仓 > 持仓天数阈值 |

输出：`TraderProfile { wallet, tags[], confidence, sample_n, updated_ts }`  
**不**自动镜像该钱包下一笔买单。

### 2.4 `FactorDistiller` → `StrategyFactorBank`

把**多钱包习惯的共现统计**蒸馏成 **我们的** 可调因子（写入纸面策略参数 / 信号侧车），映射到现有 `pump-paper-v1`：

| 习惯观察 | 蒸馏因子（兼容 v1） | 作用方式 |
|----------|---------------------|----------|
| 高胜率 cohort 多在 mid-curve 进 | `progress_bps_min/max` 收窄或确认 | 调参建议 / A-B paper |
| quick_flip 主导 | `max_hold_sec`↓、`take_profit_pct`↓ | 纸面参数 overlay |
| graduation_chase 常亏 | 强化 `progress_bps >= 9000` 强制平 / 禁开 | 已有出场；因子加权重 |
| sniper 噪声大 | **不**跟 sniper；可选 `unique_buyers_5m` 门槛↑ | tape 过滤 |
| 大单冲击后跟风亏 | `max_impact_bps` 更严 | RiskGate 已有冲击闸 |
| buy/sell 动能比 | `buy_notional_1m / sell_notional_1m` 阈值 | 已有入场条件 |

纸面信号扩展（additive，不改冻结字段名）：

- `SignalOut.tags` 可含 `factor:curve_mid_cohort`、`habit_derived`（解释用）
- 新可选 WS/REST：`GET /api/v1/traders/{wallet}/profile`、`GET /api/v1/factors/bank`

**Guardrail 开关（默认）：**

```yaml
copy_trade_enabled: false          # 禁止按钱包镜像下单
factor_from_habits_enabled: true  # 允许蒸馏到参数/标签
autopaper: false                   # 沿用 strategy_autopaper 默认关
min_sample_n: 30                   # 样本不足不写因子
exclude_tags: [sniper]             # 默认不把 sniper 写入因子库
```

---

## 3. 「习惯 → 策略因子」流程（非盲跟）

```text
用户添加钱包
  → Snapshotter 拉链上/索引成交与持仓
  → HabitTagger 打标签（统计，非单笔指令）
  → Distiller 在 cohort 上做稳健统计（中位数/分位，剔洗盘）
  → 产出 FactorProposal { name, param_patch, evidence, sample_n }
  → 人工或 paper A/B 接受 → 写入 PumpPaperParams overlay
  → pump-paper-v1.evaluate 仍只看 mint 盘面（progress/tape/impact）
  → RiskGate → PaperBroker
```

原则：

1. **学结构，不学地址**：因子作用在 `progress_bps` / 动能 / 冲击 / 持仓时长，不在 “钱包 X 刚买就买”。
2. **样本门槛**：`sample_n`、跨 mint 分散度、`maxSingleTokenPct` 类过滤（借鉴 Solana Tracker leaderboard 思路）。
3. **洗盘/捆绑降权**：同 slot 多钱包、funding 同源、bundler 标签 → 降 confidence。
4. **纸面闭环**：任何因子变更只影响 SignalOut / 参数；实盘路径保持 default off。

---

## 4. Blockers

| 阻塞 | 说明 |
|------|------|
| 无官方 REST | 前端 API unofficial；依赖即 ToS + 脆变 |
| 刮取脆 | CF / JWT / Origin；GO 条款明确禁爬 |
| 归因噪声 | ATA、路由聚合器、MEV tip 账户、多签 |
| 洗盘 / 捆绑 | 假量抬胜率；需 funding / 同 tx 聚类 |
| 延迟 | 公共 RPC 慢；索引商延迟秒～分钟 |
| PnL 定义不一致 | “赢率”需自建（已平仓、成本基础）；勿盲信第三方 |
| 成本 | Helius / Bitquery / Solana Tracker 付费；Free tier 不够盯多钱包 |
| License | 勿嵌 AGPL Geyser；Yellowstone 仅外部服务可选 |

---

## 5. P0（1–2 周）vs Later

### P0（纸面可验收）

1. Doc：本文件 + `contracts.md` 增补 `TraderSnapshot` / `HabitTag`（additive）。
2. `TraderWatchlist` CRUD + env 播种；UI 简单列表（可先 API-only）。
3. Provider：`HeliusTraderReader`（或纯 RPC）— 仅用户列表；解码 Pump buy/sell；写 SQLite/JSONL journal。
4. `HabitTagger` 规则版（上表启发式）+ `GET .../profile`。
5. `FactorDistiller` stub：输出 `FactorProposal` JSON；**手动**贴到 `PumpPaperParams`；`copy_trade_enabled=false` 硬编码。
6. 测试：固定 fixture txs → 稳定 tags；禁止路径上出现 `sendTransaction`。
7. Cloud Agent 下一步：实现 `apps/api/app/traders/` 包骨架 + 单测；不接 live broker。

### Later

- Solana Tracker / Bitquery 作为 **可选** enrich（key in env）。
- Cohort 自动 A/B 写入参数 + Journal 对比。
- Creator fee / KOL leaderboard 导入（仍转因子，不跟单）。
- GMGN 等仅在有正式 ToS/API key 后评估。
- 实时 `subscribeAccountTrade` 推送 → Snapshot 增量。

---

## 6. 工程落点（Cloud Agent / 仓库）

| 路径 | 动作 |
|------|------|
| `docs/trader-observe-paper-v0.md` | 本规格（已建） |
| `docs/strategies/pump-paper-v1.md` | 增一节「因子 overlay / 禁止 copy-trade」 |
| `docs/contracts.md` | additive：`TraderSnapshot`、`HabitTag`、`FactorProposal` |
| `apps/api/app/traders/` | `watchlist.py` / `snapshot.py` / `habits.py` / `distill.py` |
| `apps/api/app/routes/traders.py` | REST |
| `Settings` | `copy_trade_enabled=false`；第三方 API keys 仅 env |
| 禁止 | 依赖 `frontend-api*.pump.fun`；刮 Photon/BullX；实盘 mirror |

**验收：** 用户贴 3 个钱包 → 出 profile tags → 出一条 `FactorProposal`（如建议 `progress_bps_min=1200`）→ paper 策略参数可 overlay → Journal 有记录 → **零**链上签名。

---

## 7. 一句话结论

公开可观察性 **足够做纸面研究**：优先 **用户自选钱包 + 链上/Helius**，辅以 **有文档的索引商**；**不要**把 unofficial Pump 前端 API 或终端刮取当底座。产品默认 **蒸馏习惯为 curve/动量/冲击因子**，明确 **关闭盲跟钱包**。
