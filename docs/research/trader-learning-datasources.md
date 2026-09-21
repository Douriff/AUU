# AUU 交易员学习数据源 · 完整版

> 2026-09-21 Asia/Shanghai · 望舒拍板边界  
> 产品：**学习型因子**（观察 → 习惯 → 蒸馏 → 自有纸面策略）；**非盲跟单**；默认不自动镜像钱包下单  
> 冻结接口：`docs/adapters/trader-watch-distill-v0.md`（`TraderSnapshot` / `HabitTags`）  
> 已有对接：Watchlist · `new_token` · `ctx.pump` · `pump-paper-v1` · `PaperTradeJournal` · `RiskGate`

---

## 0. Pump.fun vs pump.com

| 名称 | 判定 | 说明 |
|------|------|------|
| **Pump.fun**（pump.fun） | **主场 · 已核实** | Solana 模因发射台；bonding curve → 毕业至 PumpSwap；官方公开 **程序 / IDL / SDK**，**无官方「交易员数据 API」** |
| **pump.com** | **待核实** | 公开检索未见独立成熟的另一套模因发射产品文档；口语常混称为 Pump.fun。接入前确认落地页/合约，**勿默认等同** |
| **$PUMP** | 平台代币 | ≠ 数据源 |

**结论（望舒）：** 无官方 Pump「交易员主页 / 排行榜 / 持仓」托管 API。合法只读路径 = **链上公开数据**（自选钱包 + RPC / Helius）± 有 Key 的商业 indexer（后置 enrich）。

链上锚点（以本仓 IDL 最终锁定为准；若与旧笔记不一致标待核实）：

- Pump bonding program（社区/Bitquery 常用）：`6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P`
- PumpAMM：`pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA`

---

## 1. 数据源边界（望舒拍板 · 必须遵守）

### 1.1 P0 只读（生产默认）

| 项 | 规则 |
|----|------|
| 观察对象 | **仅用户自选钱包**（`TraderWatchlist`）；不接第三方「智能钱自动关注」当默认 |
| 成交 / 持仓 | **链上 RPC 和/或 Helius**（parsed tx、token balances / wallet history） |
| progress / phase | **本仓 `ctx.pump`**（曲线账户 + 毕业状态），与 `pump-paper-v1` 同源 |
| 官方 Pump 交易员 API | **不存在** → 不假装有 |

### 1.2 可选 enrich（后置 · 需 Key · 非阻塞）

| 源 | 用途 | 前提 |
|----|------|------|
| **Solana Tracker** | 排行榜冷启动候选、钱包 trades/PnL 对照 | 付费/免费 Key；人工审核后再入 Watchlist |
| **Bitquery** | 按地址聚合 DEXTrades、top traders 研究查询 | API Token；按套餐限流 |

Birdeye / Cielo / Bubblemaps / DexScreener / Solscan / Dune：**调研级可选**，不进 P0；需要时另开 ADR。

### 1.3 硬禁止

| 禁止 | 原因 |
|------|------|
| 刮取 **frontend-api** / `frontend-api-v3.pump.fun` 等非官方前端主机 | Pump Terms 禁未授权爬取/绕过控制；无稳定性承诺 |
| 刮取 **Photon / BullX / GMGN** 网页或未授权 XHR | ToS / 鉴权；AUU 不做绕过指南 |
| 盲跟单实盘镜像、盗刷、抢跑 sniper | 产品与合规边界 |
| 把观察地址下一笔直接当 `OrderIntent` | distill-v0 明文禁止 |

可对外说明：「前端 XHR 常违 ToS，不建议」——**不提供**绕过步骤。

### 1.4 ToS 摘要（Pump）

Terms（pump.fun/docs/terms-and-conditions，页内更新 **2026-09-11**）禁止：以平台未有意提供的方式用爬虫/bot/脚本抓取或监控、绕过控制、不合理负载等。  
→ **生产数据面不得依赖** Pump 前端私有/半私有 HTTP。

---

## 2. 流水线（对齐 distill-v0）

```text
TraderWatchlist（用户自选 address）
  → Watcher(readonly, source=rpc|helius)
  → TraderSnapshot
  → HabitEngine → HabitTags ∈ {sniper, mid_curve, graduation_chase, flip, bag}
  → Distiller → PumpPaperParamsPatch / FeatureWeights
  → 用户 confirm 后才 apply → pump-paper-v1 → RiskGate → PaperBroker
  → PaperTradeJournal；对照曲线仅 reference_only
```

**HabitTags 冻结名（仅此五者）：**

| 冻结名 | 同义（调研若出现则映射） |
|--------|--------------------------|
| `sniper` | early_entry, curve_sniper |
| `mid_curve` | curve_mid, mid_bonding |
| `graduation_chase` | migrate_chase, kotl_chase |
| `flip` | quick_flip, scalp |
| `bag` | holder, long_bag |

文案用「观察 / 蒸馏 / 自有策略」；禁用「跟单 / 复制交易」作产品能力名。

---

## 3. `TraderSnapshot` 字段 → P0 源映射 + ToS

> P0 = RPC / Helius + 本仓 `ctx.pump` + 本地聚合。  
> Enrich = Solana Tracker / Bitquery（可选，后置）。  
> ToS：L=低（链上公开/本仓）；M=中（商业 Key，守供应商条款）；H=高（禁刮，不接入）。

### 3.1 元数据

| 字段 | P0 怎么来 | Enrich（可选） | ToS |
|------|-----------|----------------|-----|
| `watch_id` / `address` | 本地 Watchlist | — | L |
| `asof_ts` | AUU 时钟 ms | — | L |
| `slot` | tx / `getSlot` | Bitquery Block | L |

### 3.2 持仓

| 字段 | P0 怎么来 | Enrich | ToS |
|------|-----------|--------|-----|
| `positions[].mint` | Helius/RPC token accounts | Tracker holdings | L–M |
| `positions[].symbol?` | 可选 metadata RPC / 本仓 mint 缓存 | Tracker/Bitquery meta | L–M |
| `positions[].qty` | token amount（滤 dust） | 同 | L |
| `positions[].cost_basis_sol?` | **自算**历史 Pump buy/sell 加权（Helius parsed） | Bitquery 聚合；Tracker PnL | L–M |
| `positions[].unrealized_pnl_sol?` | qty×标记价 − cost；曲线内价用 `ctx.pump` | Tracker/Bitquery | L–M |
| `positions[].hold_sec` | 自算：净多建立 ts → asof | — | L |
| `positions[].progress_bps` | **`ctx.pump` 曲线进度** | Bitquery bonding 公式对照 | L |
| `positions[].phase` | 曲线 `complete` + 是否 AMM：`curve\|graduating\|amm\|unknown` | Bitquery ProtocolName | L |
| `open_count` | count(qty>ε) | — | L |
| `gross_exposure_sol` | Σ qty×px_sol | — | L |

### 3.3 近窗行为

| 字段 | P0 怎么来 | Enrich | ToS |
|------|-----------|--------|-----|
| `recent_buys` / `recent_sells`（`TradeBrief`） | Helius parsed Pump buy/sell（或 RPC + IDL 解码），按 address 滤 | Bitquery `DEXTrades` by trader；Tracker wallet trades | L–M |
| `TradeBrief.ts` | blockTime | 同 | L |
| `TradeBrief.mint` | ix / balance mint | 同 | L |
| `TradeBrief.side` | buy\|sell | 同 | L |
| `TradeBrief.sol_amount` | quote SOL | 同 | L |
| `TradeBrief.progress_bps?` | 成交 slot 对齐 `ctx.pump` 快照（自存/回放）；无则空 | — | L |
| `TradeBrief.signature?` | tx sig | 同 | L |
| `buy_notional_1h` / `sell_notional_1h` | 上表 1h 聚合 | Bitquery/Tracker | L–M |
| `trade_count_1h` | count | 同 | L |
| `median_hold_sec_24h` | 24h 已平仓 round-trip 持有时长中位数 | — | L |
| `flip_rate_24h` | 短持平仓 / 总平仓（阈值与 HabitEngine 一致，如 &lt;300s） | — | L |

### 3.4 进度分布 / 入场

| 字段 | P0 怎么来 | Enrich | ToS |
|------|-----------|--------|-----|
| `progress_hist` 桶 `0_800`…`migrated` | 持仓当前 progress + 近买 mint 的入场 progress 直方图 | — | L |
| `entry_progress_median_bps` | 近买入事件 progress 中位数 | — | L |

桶边界冻结（distill-v0）：`0_800` · `800_5000` · `5000_7500` · `7500_9000` · `9000_10000` · `migrated`。

---

## 4. P0 源说明

### 4.1 用户自选钱包 + Solana RPC

- **能借：** `getSignaturesForAddress` / `getTransaction`、token accounts、账户数据  
- **适合模因曲线：** 高——指令与曲线 PDA 即真相  
- **接到 AUU：** `Watcher(source=rpc)` → 解码器版本与 `pump-paper-v1` 锁定同一 IDL  
- **ToS：** 公共/自有 RPC 限流；勿把节点 Key 暴露前端  

### 4.2 Helius（推荐 P0 增强）

- **能借：** [getTransactionsForAddress](https://www.helius.dev/docs/rpc/gettransactionsforaddress)、Parsed Pump 事件、Wallet History / balances  
- **适合模因：** 高——少写原始解析样板  
- **接到 AUU：** 与 RPC 同一 Snapshot 管道；失败可降级纯 RPC  
- **ToS：** 守 Helius 订阅条款；Key 仅服务端  

### 4.3 本仓 `ctx.pump` / `new_token`

- **能借：** `progress_bps`、`phase`、virtual/real reserves、graduation  
- **角色：** Snapshot 内曲线字段的 **唯一 P0 权威**（第三方仅对照）  

---

## 5. 可选 enrich（后置）

### 5.1 Solana Tracker ★★★★☆（成熟度：中高 · 需 Key）

- **能借：** PnL leaderboard、wallet trades/holdings；声明覆盖 Pump.fun  
- **适合模因：** 高（排行榜冷启动）  
- **AUU：** 候选地址列表 → **人工**加入 Watchlist；不自动交易  
- **ToS：** 商业 API；Free 额度低（以官网为准，待核实最新档）  

### 5.2 Bitquery Pump.fun API ★★★★★（成熟度：高 · 需 Token）

- **文档声明：** Pump.fun **无**托管数据 API；Bitquery 读链  
- **能借：** 按 trader 聚合、top traders、bonding progress、新币流  
- **AUU：** 研究查询 / 回填 `recent_*`；不替代 Helius 持仓真相  
- **ToS：** 商业条款 + 套餐 RPM  

### 5.3 明确不进生产适配器

| 项 | 星（学习价值） | 处理 |
|----|----------------|------|
| GMGN 网页/未授权 API | — | **禁止刮取**；有官方 Key 的合作 API 另议，**本版不接** |
| Photon / BullX | — | **禁止刮取** |
| Pump frontend-api / advanced-api 社区逆向 | ★☆☆☆☆ | **禁止**；仅存在性记录 |
| DexScreener | ★★☆☆☆ | 无钱包成交主 API；AMM 价可选，非 Snapshot P0 |
| Bubblemaps / Solscan | ★★★☆☆ | 集群/资助风险注释；非本版范围 |

---

## 6. 与 AUU 模块粘合

| 已有 | 角色 |
|------|------|
| Watchlist | 并行/升格 `TraderWatchlist`（无私钥） |
| `new_token` / `ctx.pump` | progress / phase |
| `pump-paper-v1` | 蒸馏目标（params patch + weights） |
| `PaperTradeJournal` | 自有纸面；`trader_ref` = reference_only |
| `RiskGate` | 纸面单仍过闸；观察模块永不 live |

蒸馏默认：**不**自动 apply；**不**改 `auto_paper_orders`；`sniper` / `graduation_chase` 主导时 distill-v0 建议拒绝或仅警示。

---

## 7. 里程碑（建议）

| 阶段 | 内容 |
|------|------|
| M0 | Watchlist CRUD + Snapshot mock |
| M1 | **P0：** Helius/RPC → 真实 Snapshot |
| M2 | HabitEngine 五标签 + Distill（confirm 才 apply） |
| M3 | 可选：Tracker 排行榜候选 / Bitquery 回填 |
| — | 永不：frontend-api / Photon / BullX / GMGN 刮取 |

---

## 8. 待核实

1. pump.com 是否解析到非 Pump.fun 产品  
2. 本仓 IDL 中 Pump program id 与上文常量最终锁定  
3. Solana Tracker / Bitquery 现行套餐与 leaderboard 是否含纯曲线期成交  
4. Helius parsed Pump 事件字段与本仓解码器字段对齐表  

---

## 9. 参考

- distill-v0：`docs/adapters/trader-watch-distill-v0.md`  
- Pump Terms：https://pump.fun/docs/terms-and-conditions  
- pump-public-docs：https://github.com/pump-fun/pump-public-docs  
- Helius：https://www.helius.dev/docs/  
- Bitquery Pump.fun API：https://docs.bitquery.io/docs/blockchain/Solana/Pumpfun/Pump-Fun-API/  
- Solana Tracker：https://www.solanatracker.io/data-api  

*方法：WebSearch/WebFetch。不确定已标待核实。禁刮源未给绕过步骤。*
