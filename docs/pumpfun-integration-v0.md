# Pump.fun 集成调研 · AUU 纸面可视化 v0

状态：调研规格（2026-09-21，Asia/Shanghai）。范围：**只读行情 + 纸面撮合可视化**；禁止实盘密钥、狙击、掏空/drain 模式。  
对齐：`docs/contracts.md`、`docs/market-terminal-scaffold-v0.md`、`apps/api/app/providers/base.py`。

---

## 1. Bonding Curve → Migration 概览

### 1.1 生命周期

```text
create / create_v2
    → BondingCurve 交易（buy / sell）
    → real_token_reserves == 0  ⇒ complete=true（毕业就绪）
    → migrate（permissionless）
    → PumpSwap AMM（program: pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA）
```

| 阶段 | 场所 | 关键状态 |
|------|------|----------|
| 发射 | Pump program `6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P` | PDA `["bonding-curve", mint]` |
| 曲线交易 | 同一 program 的 `buy` / `sell` | 虚拟储备驱动价格；真实储备跟踪可卖量 |
| 毕业就绪 | `complete == true` 且 `real_token_reserves == 0` | 曲线停止可买完；等待迁移 |
| 迁移 | `migrate(user, mint)` 幂等、无权限 | 流动性进 PumpSwap；LP 销毁 |
| 后毕业 | PumpAMM | 用 `canonicalPumpPoolPda(mint)`；买卖走 AMM ix |

官方说明见：[pump-fun/pump-public-docs](https://github.com/pump-fun/pump-public-docs) → `docs/PUMP_PROGRAM_README.md`（仓库无 SPDX license 声明，文档只读引用）。

### 1.2 曲线数学（Uniswap V2 风格合成储备）

典型 `Global` 初值（主网可查 PDA `4wTV1YmiEkRvAtNtsSGPtUrqRYQMe5SKy2uB4Jjaxnjf`）：

| 字段 | 约值 | 含义 |
|------|------|------|
| `initial_virtual_sol_reserves` | 30 SOL | 合成 SOL 侧 |
| `initial_virtual_token_reserves` | ~1.073e15 | 合成 token 侧（6 位小数） |
| `initial_real_token_reserves` | ~7.931e14 | 曲线上可卖真实 token（约 793.1M） |
| `token_total_supply` | 1e15 | 总供给 1B（6 位） |
| `fee_basis_points` | 100 bps | 协议费基线（另有 creator / tier 费用演进） |

现货价（粗算，忽略费）：

\[
P_{\text{SOL/token}} \approx \frac{\text{virtual\_sol\_reserves}}{\text{virtual\_token\_reserves}}
\]

**进度（毕业进度）**常用两种等价观测量（AUU 应同时暴露，前端选一种画条）：

1. **真实 token 耗尽**（协议真相）：  
   `progress = 1 - real_token_reserves / initial_real_token_reserves`  
   当 `real_token_reserves == 0` → 100%。
2. **社区常用近似**（Bitquery 等）：  
   `100 - ((base_balance - 206_900_000) * 100 / 793_100_000)`（单位为「百万 token」量级，实现时用链上 raw 单位）。  
3. **真实 SOL 蓄水**：毕业时 `real_sol_reserves` 常约 **~85 SOL**（经验值，非硬编码常量；以 `complete` 为准）。

迁移后：约 **206.9M** 剩余 token + 蓄积 SOL 注入 PumpSwap 恒定乘积池；历史曾迁 Raydium，当前默认 **PumpSwap**。

### 1.3 AUU 可视化语义

- K 线 / trades：曲线期用曲线成交；`complete` 后若已 migrate，切到 AMM 池成交（或标 `phase: "amm"`）。
- Progress bar：绑 `progress_bps` + `complete` + `migrated`。
- 纸面成交：用曲线公式本地模拟 fill（见 §3），**不**向链上 `buy`/`sell`。

---

## 2. 公共 API / SDK / 链上监听（含 GitHub + License）

> 原则：AUU v0 只消费**公开读路径**；第三方 API Key 若将来需要，仅放本机 env，**永不入库**。

### 2.1 官方 / 半官方

| 资源 | 链接 | License | AUU 用途 |
|------|------|---------|----------|
| Pump 程序公开文档 | https://github.com/pump-fun/pump-public-docs | 未声明 SPDX（只读） | 曲线/迁移语义权威来源 |
| 官方 TS SDK `@pump-fun/pump-sdk` | npm：https://www.npmjs.com/package/@pump-fun/pump-sdk （repo 声明 `pump-fun/pump-sdk`） | **MIT** | 解码账户、报价公式、IDL；**勿**接实盘 send |
| PumpSwap SDK `@pump-fun/pump-swap-sdk` | npm 依赖自官方 SDK | 随包（见 npm） | 毕业后池状态 / AMM 报价参考 |

说明：`pump-fun/pump-sdk` 的 GitHub 页面可能对未授权访问返回 404；以 **npm MIT 包 + public-docs** 为准做集成。

### 2.2 社区 SDK（可读状态 / 指令构建）

| 资源 | 链接 | License | 备注 |
|------|------|---------|------|
| nirholas/pump-fun-sdk（`@nirholas/pump-sdk`） | https://github.com/nirholas/pump-fun-sdk | README 称 **Apache-2.0**（GitHub license API 现为 NOASSERTION，引用前再核 `LICENSE`） | `fetchBondingCurveSummary` / `progressBps` / 离线报价；CLI `pump curve` |
| nhuxhr/pumpfun-rs（crate `pumpfun`） | https://github.com/nhuxhr/pumpfun-rs | **Apache-2.0**（兼 MIT 声明） | Rust 查曲线、WS 事件；AUU 后端若用 Python 可只抄公式 |
| 0xfnzero/pumpfun-sdk | https://github.com/0xfnzero/pumpfun-sdk | **MIT** | Yellowstone / logs；含 Jito 等提交路径 → **AUU 禁止借用提交/bundle 部分** |

### 2.3 第三方数据 API（索引，非链直连）

| 资源 | 链接 | 鉴权 | AUU 用途 |
|------|------|------|----------|
| PumpPortal Data WS | https://pumpportal.fun/data-api/real-time/ · 示例 https://github.com/thetateman/Pump-Fun-API | WS；部分流需 API key / 计量 | `subscribeNewToken` 只读。URI `wss://pumpportal.fun/api/data?api-key=...`。**HTTP 400** = 畸形/拼接 key；**403** = 无效/过期/封禁 key 或 IP 禁（文档要求同一时间一条 WS）。AUU：指数退避（封顶 ~5min）+ `discoveryReason=portal_auth_rejected`；有 `SOLANA_RPC_URL` 时可回退 `logs`。见 `docs/adapters/pumpportal-discovery-v0.md` |
| Bitquery Pump.fun GraphQL | https://docs.bitquery.io/docs/blockchain/Solana/Pumpfun/ | API key | 曲线进度、池余额查询 |
| Solana Tracker Pump API | https://www.solanatracker.io/pumpfun-api | API key | REST + WS `curvePercentage` / graduated |
| Codex Launchpad | https://docs.codex.io/launchpads/pump-fun | API key | `graduationPercent` / migrated 事件 |

**纸面默认建议**：不依赖付费索引；优先 **公共 Solana RPC** `getAccountInfo` + `logsSubscribe` / `programSubscribe`。索引作 P1 可选增强。

### 2.4 链上监听路径（纸面安全）

| 方式 | 做法 | 依赖 License / 风险 |
|------|------|---------------------|
| **A. RPC accountSubscribe** | 订阅 bonding-curve PDA；Anchor/Borsh 解码 `BondingCurve` | `@solana/web3.js` Apache-2.0；最简 |
| **B. logsSubscribe** | `mentions: [PUMP_PROGRAM_ID]`，解析 `TradeEvent` / `CreateEvent` / `CompleteEvent` | 同上；需自写 discriminator 解析 |
| **C. Yellowstone gRPC** | 账户/交易过滤 owner=`6EF8…` | [rpcpool/yellowstone-grpc](https://github.com/rpcpool/yellowstone-grpc) **AGPL-3.0** → AUU **不嵌库**；若用仅作外部托管流的客户端协议，合规需法务确认 |
| **D. 第三方 WS** | PumpPortal / Solana Tracker | 商业条款；无密钥入库 |

**AUU v0 选型**：**A + B（公共 RPC）**；解码优先参考官方 SDK 类型布局，Python 侧自研轻量 decoder（避免 GPL/AGPL 污染）。

Program ID 速查：

- Pump：`6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P`
- PumpAMM：`pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA`
- PumpFees：`pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ`

---

## 3. AUU `MarketDataProvider` · `pumpfun_paper` 字段设计

### 3.1 Provider 插槽

当前：`DATA_PROVIDER=mock`；前端 `DataSource = "mock" | "paper"`（下单路径）。  
新增后端 provider 名：**`pumpfun_paper`**（行情源）；下单仍走现有 `PaperBroker` + `RiskGate`，**永不** `sendTransaction`。

```text
DATA_PROVIDER=mock | pumpfun_paper
# paper 仍表示「纸面下单模式」，与行情源正交；Settings 可显示：
#   market=pumpfun_paper, orderMode=paper
```

WS `hello.providers` 扩为：`["mock","pumpfun_paper"]`（保持 `version:1`；只加不改）。

### 3.2 扩展合同（只增字段，不改名）

在冻结 `Candle` / `Fill` / `SignalOut` 之外，增加可选载荷（REST 快照 + WS 推送）：

```ts
/** WS/REST 附加：Pump.fun 曲线纸面行情 */
interface PumpfunPaperSnapshot {
  mint: string;                 // base58
  symbol: string;               // AUU 自选符号，如 "BONKMOCK/SOL" 或真实 ticker
  phase: "curve" | "graduating" | "amm";
  // 进度
  progress_bps: number;         // 0..10000
  complete: boolean;
  migrated: boolean;
  // 储备（字符串防 JS 精度丢失；UI 再格式化）
  virtual_sol_reserves: string;   // lamports
  virtual_token_reserves: string; // raw
  real_sol_reserves: string;
  real_token_reserves: string;
  token_total_supply: string;
  // 价格
  price_sol: number;            // spot，已换算为 SOL/token（可读 float；大数路径另给 price_sol_str）
  price_sol_str?: string;
  market_cap_sol?: number;
  // 池（毕业后）
  pool?: string;                // PumpAMM pool PDA
  // 元数据
  slot?: number;
  updated_ts: number;           // unix ms
}

interface PumpfunTradeTick {
  mint: string;
  symbol: string;
  ts: number;
  side: "buy" | "sell";
  price: number;
  qty: number;                  // token raw → UI 用 decimals=6
  sol_amount: number;
  signature?: string;           // 链上签名仅作溯源展示；纸面 Fill 不复用
  phase: "curve" | "amm";
}
```

映射到现有通道：

| 现有 channel | `pumpfun_paper` 行为 |
|--------------|----------------------|
| `candles` | 由 trade tick 聚合 1s/1m OHLCV（或 REST 历史空 → 实时滚） |
| `trades` | `PumpfunTradeTick` 投影为现有 `TradeTick`，扩展字段放 `meta` 或并行 `type:"pumpfun_curve"` |
| `book` | 曲线期用合成盘口：按公式在 mid±几档估算可吃量（标注 `synthetic:true`） |
| `signals` / `fills` / `risk` | 仍来自 demo 策略或 PaperBroker；**与链上签名解耦** |

`SymbolInfo` 建议：

```ts
{ symbol, base, quote: "SOL", kind: "pumpfun_curve", mint?: string }
```

### 3.3 Python Provider 草图

```python
# apps/api/app/providers/pumpfun_paper.py
class PumpfunPaperProvider(MarketDataProvider):
    name = "pumpfun_paper"

    def list_symbols(self) -> list[SymbolInfo]: ...
    def get_candles(...): ...          # 内存 ring buffer
    async def stream(self, channel, symbol, interval=None):
        # channel=="trades" → 解码日志/账户更新
        # 另允许内部推 type=pumpfun_curve 的 snapshot
```

曲线报价（纸面 fill 用，与 RiskGate `LiquidityCtx` 衔接）：

- `price_sol = virtual_sol / virtual_token`
- 买入冲击：`buy_tokens_out` + 费后 SOL（`sol_after_buy_fee`）；卖出冲击：`sell_sol_out`。两侧分叉，不得写成一支带符号的恒定乘积。
- `impact_bps = max(成交均价相对 mid0, mid 移动) + fee_bps/2`（默认 fee **125 bps**，可覆盖）
- `complete` / `migrated` **只作闸**：进度 ≥95% 或毕业时 RiskGate 已有 impact ×1.5；不要把这两个布尔塞进 K。
- 无 `ctx.pump` / 曲线储备时：保持原 CEX 平方根冲击。
- `progress_bps = int(10_000 * (1 - real_token / initial_real_token))`

---

## 4. Paper-safe vs 钱包必需 · 禁止模式

### 4.1 分层

| 能力 | 钱包？ | AUU v0 |
|------|--------|--------|
| 读 Global / BondingCurve / 池 | 否 | ✅ |
| 订 logs / account 更新 | 否（RPC URL） | ✅ |
| 第三方只读 WS | 否或仅 API key | ⚠️ 可选，env only |
| 纸面下单 `POST /paper/orders` | 否 | ✅ 已有 |
| 链上 `buy`/`sell`/`migrate`/`create` | **需要** | ❌ 禁止本轮 |
| Jito / 0slot / bundle / 抢块 | 需要 | ❌ 永久禁止产品路径 |
| 私钥、助记词、session drain | — | ❌ 禁止出现在代码/文档示例 |

### 4.2 明确禁止（代码审查清单）

- **狙击（sniper）**：监听 `create` 后自动实盘买；任何 `GRPC_ENDPOINT` + `WALLET_PRIVATE_KEY` 组合模板。
- **Drain / 掏空**：批量归集 ATA、恶意 approve、伪装「一键卖出」转走 SOL。
- **MEV 抢跑**：同区块捆绑买卖、优先费战争实盘脚本。
- **复制 sniper 仓库可执行路径**：可参考其「事件解码」思想，**不得**引入其 executor / keypair 加载。

RiskGate 可预留 tags（仅纸面告警）：`MEV_SUSPECT`、`HONEYPOT_FLAG`、`CURVE_NEAR_GRADUATION`（进度 > 95% 时加大滑点假设）。

### 4.3 `.env.example` 约束

```bash
DATA_PROVIDER=mock
# 将来：
# DATA_PROVIDER=pumpfun_paper
# SOLANA_RPC_URL=https://api.mainnet-beta.solana.com
# PUMPFUN_WATCH_MINTS=   # 逗号分隔，白名单；空=仅内置演示 mint
# PUMPFUN_DISCOVERY=off  # pumpportal|logs|off；无 PUMPFUN_PORTAL_API_KEY 时默认 off
# PUMPFUN_PORTAL_API_KEY=  # 仅 env，subscribeNewToken 只读
# 禁止：PRIVATE_KEY / WALLET_SECRET / API 交易密钥 / sniper
```

---

## 5. 可复用 OSS 优先列表（借思想 / 解码，不借实盘执行）

| 优先级 | 仓库 | License | 借什么 | 不借什么 |
|--------|------|---------|--------|----------|
| P0 | [pump-fun/pump-public-docs](https://github.com/pump-fun/pump-public-docs) | 未声明 | 状态机、字段语义 | — |
| P0 | `@pump-fun/pump-sdk`（npm MIT） | MIT | BondingCurve 布局、报价函数名与公式 | 任何 `sendAndConfirm` 示例当产品默认 |
| P0 | [nirholas/pump-fun-sdk](https://github.com/nirholas/pump-fun-sdk) | Apache-2.0（再核 LICENSE） | `progressBps`、summary、事件解码示例 | Telegram bot / 实盘 CLI 默认路径 |
| P1 | [nhuxhr/pumpfun-rs](https://github.com/nhuxhr/pumpfun-rs) | Apache-2.0 / MIT | 账户类型、WS 事件形状 | create/buy 签名流 |
| P1 | Solana Tracker 文档示例（账户流） | 文档 | Yellowstone filter 形状（若未来合规允许） | 其商业 gRPC 强绑定 |
| 参考勿嵌 | [0xfnzero/pumpfun-sdk](https://github.com/0xfnzero/pumpfun-sdk) | MIT | logs 订阅结构 | Jito/0slot/抢跑提交 |
| **禁止作依赖** | [Kernlog/pump-sniper](https://github.com/Kernlog/pump-sniper) 等 sniper | Apache-2.0 等 | 无（仅知「业界有此反模式」） | 整仓逻辑 |
| **许可谨慎** | [rpcpool/yellowstone-grpc](https://github.com/rpcpool/yellowstone-grpc) | **AGPL-3.0** | 不 vendoring | 不链进 AUU 发行物 |

AUU 总原则（与 README 一致）：优先 **MIT / Apache-2.0**；不 fork GPL 前端；不嵌 AGPL 服务端库。

---

## 6. 下一实现切片（具体文件）

目标：**只读公共 RPC + 内存聚合 + 纸面 broker 仍可用**；一个 mint 白名单可在 MarketPage 看到进度条与 trades。

### 6.1 后端

| 文件 | 动作 |
|------|------|
| `apps/api/app/providers/base.py` | 保持 ABC；可选加 `async def get_pumpfun_snapshot(mint) -> dict` |
| `apps/api/app/providers/pumpfun_paper.py` | **新建**：RPC 拉曲线、解码、进度/价格、trade 缓冲 |
| `apps/api/app/providers/pumpfun_decode.py` | **新建**：BondingCurve borsh/布局常量（自 public-docs / SDK 对照） |
| `apps/api/app/providers/pumpfun_curve_math.py` | **新建**：spot、progress_bps、纸面 buy/sell 冲击 |
| `apps/api/app/providers/__init__.py` / `mock.py` 的 `get_provider()` | `DATA_PROVIDER=pumpfun_paper` 分支 |
| `apps/api/app/models/contracts.py` | 增加 `PumpfunPaperSnapshot` / `PumpfunTradeTick`（Pydantic） |
| `apps/api/app/routes/ws.py` | `hello.providers` 含 `pumpfun_paper`；可推 `type:"pumpfun_curve"` |
| `apps/api/app/routes/symbols.py` | kind=`pumpfun_curve` |
| `apps/api/app/routes/health.py` | `dataSourceOptions` 增加展示名 |
| `.env.example` | `SOLANA_RPC_URL`、`PUMPFUN_WATCH_MINTS` 注释项 |

### 6.2 前端

| 文件 | 动作 |
|------|------|
| `apps/web/src/types/contracts.ts` | 镜像上述类型；`DataSource` 暂不动或加注释「行情源见 health」 |
| `apps/web/src/providers/HttpWsProvider.ts` | 处理 `pumpfun_curve` 帧 |
| `apps/web/src/components/market/CurveProgressBar.tsx` | **新建**：吃 `progress_bps` / `complete` / `migrated` |
| `apps/web/src/pages/MarketPage/MarketPage.tsx` | 挂进度条；TradesTape 显示 buy/sell |
| `apps/web/src/pages/SettingsPage/SettingsPage.tsx` | 展示 `DATA_PROVIDER` 只读 + 纸面声明 |

### 6.3 文档 / 验收

| 文件 | 动作 |
|------|------|
| `docs/contracts.md` | 追加 Pumpfun* 字段表 |
| `docs/architecture.md` | 图中加 `PumpfunPaperProvider` |
| `README.md` | Mock / pumpfun_paper 对照一行 |

**验收（本切片）**

- [ ] `DATA_PROVIDER=pumpfun_paper` 时 WS hello 含该 provider
- [ ] 至少 1 个 watch mint 显示 `progress_bps` 与非空储备字段
- [ ] trades tape 有 buy/sell；K 线能滚动（允许冷启动空白数秒）
- [ ] `POST /paper/orders` 仍只出纸面 Fill；进程内无 `Keypair` / 无私钥文件
- [ ] 仓库无 sniper/Jito/drain 依赖与示例

**明确不做（本切片）**：链上买卖、自动 migrate、Yellowstone 强依赖、付费索引强制、多 mint 洪流扫描器。

---

## 附录 A · 参考链接速表

- https://github.com/pump-fun/pump-public-docs  
- https://www.npmjs.com/package/@pump-fun/pump-sdk  
- https://github.com/nirholas/pump-fun-sdk  
- https://github.com/nhuxhr/pumpfun-rs  
- https://pumpportal.fun/data-api/real-time/  
- https://docs.bitquery.io/docs/blockchain/Solana/Pumpfun/Pump-Fun-Marketcap-Bonding-Curve-API/  

## 附录 B · 版本

- 文档版本：`pumpfun-integration-v0`
- 日期：2026-09-21（UTC+8）
- 作者角色：调研执行（无实盘密钥）
