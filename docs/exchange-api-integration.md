# 外部交易所 API 接入方案（Binance / OKX / Bybit / Bitget）

状态：**方案草稿，未实现，未合并，未部署**。日期：2026-10-13。
不变的安全规则：私钥/API Secret 不进仓库、不硬编码；真实交易只用站长本人资金；`liveEnabled` 默认 `false`，站长确认前保持关闭；所有相关功能仅 admin 可用。

---

## 0. 现状（代码盘点）

| 模块 | 现状 |
|---|---|
| `apps/api/app/marketdata/mainstream/`（fetcher/orderbook/recon/service） | 已用 **ccxt==4.5.85**（`requirements.txt`），**无 key** 调 Binance/OKX 公共 REST；provider = `cex_public`；有重试、分页、封禁检测、ccxt 内置限速，orderbook 复用同一 client 和限速器 |
| `apps/api/app/live/`（gate.py / broker.py / send.py / signer.py / ledger.py） | 针对 Pump.fun/Solana 的 LiveBroker **桩**；硬闸门 fail-closed：`liveEnabled` + 二次确认 + 本地密钥挂载 + LiveLimits 四项全满足才 armed；`LIVE_SEND_WIRED=False`，即使 armed 也不发送 |
| `apps/api/app/risk/gate.py` | `RiskGate.check`（paper）与 `check_live`（独立 LiveLimits）分离，借鉴 vn.py RiskManager |
| `apps/api/app/paper/` | PaperBroker、mainstream_account、ledger、exec_shadow、reconcile 类逻辑已存在 |
| 测试 | `tests/test_mainstream_data.py` mock 掉 ccxt 且禁网；断言 `PUT /api/v1/live/enabled` 不能打开 live |

结论：**行情层（公共数据）已就绪；私有数据（余额/持仓/订单）与 CEX 下单完全没有**。新增 CEX 私有接入应复用现有 live gate 的"fail-closed + 二次确认 + 独立限额"模式，而不是另起一套。

---

## 1. 公共 API vs 私有 API

| 数据 | 类型 | 需要 key | 说明 |
|---|---|---|---|
| K 线、ticker、深度、成交、资金费率、标记价格、持仓量、合约规格 | 公共 | 否 | 已有（cex_public）。可补 WebSocket 推送降低 REST 压力 |
| 账户余额（现货/统一账户/合约） | 私有 | 只读 key | 阶段 1 目标 |
| 持仓（合约仓位、杠杆、强平价） | 私有 | 只读 key | 阶段 1 |
| 当前挂单、历史订单、成交明细、手续费等级 | 私有 | 只读 key | 阶段 1，用于对账与真实成本建模 |
| 资金流水（充提、划转、资金费） | 私有 | 只读 key | 用于 PnL 归因 |
| 下单/撤单/改单、调杠杆 | 私有 | **交易** 权限 | 阶段 3 起，仅站长 |
| 提现 | 私有 | 提现权限 | **永远不申请、不支持** |

---

## 2. ccxt vs 原生 SDK

| | ccxt（MIT） | 原生 SDK / 直连 REST+WS |
|---|---|---|
| 覆盖 | 4 家统一接口（`fetch_balance`/`fetch_positions`/`create_order`…），项目已在用 | 每家一套，字段差异大 |
| 新功能 | 略滞后；特殊参数用 `params={}` 透传 | 第一时间 |
| WebSocket | 需 ccxt.pro（现已并入 ccxt 免费包，`ccxt.pro` 命名空间） | 官方 WS 文档最权威 |
| 认证细节 | 自动处理签名、OKX passphrase、Bybit recvWindow | 自行实现 |
| 风险 | 统一抽象可能掩盖差异（统一账户、对冲模式、clientOrderId 规则） | 维护成本高 |

**建议**：私有 REST 与下单统一走 **ccxt**（与现有行情层一致，锁版本、升级前跑 mock 测试）；私有 WS（订单/成交/持仓推送）先用 ccxt.pro，若稳定性不足再对单家改成官方 WS 直连。所有调用包在自研 `Connector` 接口后面，上层不直接依赖 ccxt。
- ccxt 文档：https://docs.ccxt.com/  · GitHub：https://github.com/ccxt/ccxt
- Binance 官方连接器：https://github.com/binance/binance-connector-python
- OKX 官方 SDK 列表：https://www.okx.com/docs-v5/en/#overview-sdk
- Bybit 官方 pybit：https://github.com/bybit-exchange/pybit
- Bitget 官方 SDK：https://github.com/BitgetLimited/v3-bitget-api-sdk

---

## 3. API Key 模型

### 3.1 权限最小化
1. **先只读**（Read / 读取）key：阶段 1–2 只用这个。
2. 交易 key：仅现货/合约交易，**关闭提现**、关闭万向划转/子账户划转（如可选）。
3. **IP 白名单**绑定执行端出口 IP（服务器固定 IP，或站长笔记本若有固定出口）。Binance 的交易权限在无白名单时有有效期限制，强烈要求白名单。
4. 优先使用**专用子账户**，只放愿意用于自动交易的资金，实现资金隔离。
5. Binance 支持 **Ed25519 / RSA** 自生成密钥对（私钥只在执行端），优先于 HMAC。

官方说明：
- Binance API key 类型与 Ed25519：https://developers.binance.com/docs/binance-spot-api-docs/faqs/api_key_types
- Binance 现货 REST 安全/签名：https://developers.binance.com/docs/binance-spot-api-docs/rest-api/request-security
- OKX V5 认证（APIKey + Secret + Passphrase，权限 读取/交易/提现，IP 绑定）：https://www.okx.com/docs-v5/en/#overview-rest-authentication
- Bybit V5 认证：https://bybit-exchange.github.io/docs/v5/guide
- Bitget API 认证：https://www.bitget.com/api-doc/common/signature

### 3.2 Key 存放（三选一，按安全性从高到低）

| 方案 | 描述 | 优点 | 缺点 |
|---|---|---|---|
| **A. 站长笔记本执行器（推荐用于交易 key）** | 交易 key 只在站长本机；本机跑 executor，**主动出站**连服务器拉取"待确认订单意图"，本地签名并下单，回传结果 | 服务器被攻破也拿不到交易 key；与"私钥不上服务器"规则一致；与现有 local-signer 思路一致 | 笔记本需在线；IP 白名单需固定出口；自动化程度受限 |
| **B. 服务器 + KMS 信封加密** | 每个 key 用随机 DEK（AES-256-GCM）加密，DEK 由云 KMS（AWS KMS / GCP KMS / Vault Transit）包裹；数据库只存密文；运行时解密在内存，不落盘不打日志 | 支持 24h 自动化；支持未来多用户 | 服务器进程内存仍有明文；需云 KMS 和审计 |
| C. 环境变量 / 本地 secrets 文件 | gitignored `secrets/` 或 env | 最简单 | 不适合交易 key，只可短期用于站长**只读** key |

**建议**：只读 key → 方案 B（或过渡期 C）放服务器；交易 key → 方案 A 站长本机。如阶段 4 需 24h 自动，再评估迁至 B + 子账户 + 小额上限。
- AWS KMS 信封加密：https://docs.aws.amazon.com/kms/latest/developerguide/kms-cryptography.html#enveloping
- Google Cloud KMS 信封加密：https://cloud.google.com/kms/docs/envelope-encryption
- HashiCorp Vault Transit：https://developer.hashicorp.com/vault/docs/secrets/transit

前端规则：key 输入框只写不读（提交后只显示后 4 位与权限检测结果），录入时后端调用一次 API 查询权限，**若检测到提现权限直接拒绝保存**。

---

## 4. 架构

```
           ┌──────────── Web (admin only) ────────────┐
           │ 账户页 / 持仓 / 订单 / 对账 / Kill Switch │
           └───────────────────┬──────────────────────┘
                               │
 FastAPI ── AccountSync ── Connector(接口) ── BinanceConn / OkxConn / BybitConn / BitgetConn (ccxt)
    │           │                 │
    │       Reconciler        RateLimiter(按交易所权重/UID/IP) + WS 管理
    │
 OrderRouter ── RiskGate.check_live(CEX LiveLimits) ── LiveGate(fail-closed) ── OrderIntent 队列
                                                                                 │
                                                          方案A: 站长本机 Executor 拉取→本地签名→下单
```

### 4.1 Connector 层
统一接口：`fetch_balances / fetch_positions / fetch_open_orders / fetch_my_trades / fetch_order / create_order / cancel_order / stream_user_events`。返回规范化 dataclass（与 `app/models/contracts.py` 对齐）；交易所差异（OKX `tdMode`、Bybit `category`、Binance 现货/USDⓈ-M 分端点、Bitget `productType`）都在适配器内部处理。

### 4.2 限速
- 依据各家权重表做令牌桶，并读取响应头（Binance `X-MBX-USED-WEIGHT-*`、`X-MBX-ORDER-COUNT-*`）动态退避；收到 429/418（Binance）立即熔断该交易所；私有接口按 UID 计、公共按 IP 计，分开限速。
- Binance 限速：https://developers.binance.com/docs/binance-spot-api-docs/rest-api/limits
- OKX 限速：https://www.okx.com/docs-v5/en/#overview-rate-limits
- Bybit 限速：https://bybit-exchange.github.io/docs/v5/rate-limit
- Bitget 限速：https://www.bitget.com/api-doc/common/intro

### 4.3 WebSocket
- 公共：K 线/深度/成交推送，替代高频轮询。
- 私有：订单/成交/持仓/余额推送（Binance User Data Stream 需 listenKey 续期；OKX/Bybit/Bitget 登录签名）。断线自动重连 + 重连后用 REST 全量快照补齐。
- Binance User Data Stream：https://developers.binance.com/docs/binance-spot-api-docs/user-data-stream
- OKX WS：https://www.okx.com/docs-v5/en/#overview-websocket
- Bybit WS：https://bybit-exchange.github.io/docs/v5/ws/connect
- Bitget WS：https://www.bitget.com/api-doc/common/websocket-intro

### 4.4 订单路由与幂等
- 每个 OrderIntent 生成确定性 `clientOrderId`（如 `auu-{strategy}-{intentId}`，满足各家长度/字符限制：Binance `newClientOrderId`、OKX `clOrdId`≤32 字母数字、Bybit `orderLinkId`、Bitget `clientOid`）。
- 超时/网络错误时**不重发新单**，先用 clientOrderId 查询订单状态，确认不存在再重试。
- 意图状态机：`pending_confirm → approved → sent → acked → partially_filled → filled|cancelled|rejected|unknown`；`unknown` 进入对账。
- 只支持限价单 + 可选 reduce-only；市价单需额外滑点保护。

### 4.5 对账（Reconciliation）
- 每 1–5 分钟：交易所快照（余额/持仓/挂单）vs 本地 ledger；差异 > 阈值报警，严重差异自动触发 kill switch。
- 每日：成交明细与手续费/资金费全量对账，结果进入 PnL 报表（与 paper 统计严格分开，`source=live_cex`）。
- 可复用 `marketdata/mainstream/recon.py` 的阈值/报警思路。

---

## 5. 按用户 key vs 仅站长

| | 仅站长（建议现阶段） | 多用户各自 key |
|---|---|---|
| 合规 | 自有资金自用，风险最低 | 可能被视为提供投资/资管/跟单服务，需按地区评估牌照 |
| 安全 | key 少、可放本机 | 必须方案 B + 租户隔离 + 审计 + 渗透测试 |
| 实现 | 单租户 | `user_id` 维度隔离、每用户限速、每用户风控 |

建议：阶段 1–4 **只做站长**。多用户只读（看自己账户、同步资产）作为后续独立阶段，交易能力不对外开放，除非完成法律评估。

---

## 6. 风控与 Kill Switch

沿用 `app/live/gate.py` 的 fail-closed 模式，新增 **CEX LiveLimits**（与 paper、与 Pump.fun 的 LiveLimits 都独立）：
- 单笔最大名义（建议起步 50 USDT）、单币最大持仓、总敞口上限、最大杠杆（起步 1–2x，或仅现货）、日内最大亏损（%与绝对值）、每分钟最大下单数、价格偏离保护（下单价 vs 中间价 > x% 拒绝）、交易对白名单。
- **Kill Switch**（三层）：
  1. 软件：admin 按钮/API → 停止新意图、撤销所有挂单、可选市价/限价平仓；
  2. 自动：日亏损超限、对账差异、连续拒单、WS 断线超时、限速熔断 → 自动触发；
  3. 交易所侧：Binance/Bybit/OKX 的 cancel-all / "dead man's switch"（如 Binance 合约 `countdownCancelAll`、OKX `cancel-all-after`、Bybit DCP）——即便我方进程挂了，挂单也会被交易所撤掉。最终兜底：站长在交易所后台直接删除 API key。
- OKX cancel-all-after：https://www.okx.com/docs-v5/en/#order-book-trading-trade-post-cancel-all-after
- Binance 合约自动撤单：https://developers.binance.com/docs/derivatives/usds-margined-futures/trade/rest-api/Auto-Cancel-All-Open-Orders
- Bybit DCP：https://bybit-exchange.github.io/docs/v5/order/dcp

---

## 7. 分阶段上线

| 阶段 | 内容 | key 权限 | 准入/退出条件 |
|---|---|---|---|
| **1 只读同步（站长）** | 余额/持仓/订单/成交同步到 admin 页；对账报表 | 只读 + IP 白名单 | 连续 7 天同步无差异；key 不出现在日志/响应 |
| **2 Paper 镜像** | 策略信号同时生成 paper 单与"影子真实单"（只记录不发送），用真实盘口与真实手续费等级评估可成交性与滑点 | 只读 | 影子单与 paper 成交偏差在阈值内 ≥2 周 |
| **3 小额实盘 + 人工确认** | 每笔意图需站长在 UI/本机 executor 点击确认；极小限额；子账户 | 交易（无提现）+ 白名单，key 在站长本机 | `liveEnabled` 由站长手动打开；任何异常触发 kill switch 后需人工复位 |
| **4 自动** | 在限额内免确认自动执行；保留 kill switch 与日报 | 同上（或 KMS 方案） | 阶段 3 至少 N 笔（建议 ≥50）无对账事故、实盘与 paper 偏差可接受 |

每个阶段单独 PR，默认关闭，测试全部 mock ccxt 且禁网（延续现有测试风格）。

---

## 8. 法律 / ToS / 变现

- 本文不是法律意见。用户在中国大陆以外，但仍需按目标地区评估：若替他人下单、跟单、托管 key 或收取业绩分成，可能涉及投资顾问/资产管理/虚拟资产服务牌照（如欧盟 MiCA、英国 FCA 金融推广规则、香港 SFC、新加坡 MAS 等）。
- 各交易所 ToS 均限制受限地区用户（如美国、英国部分产品、加拿大部分省份等）；平台不得帮助用户规避地区限制。不同交易所对第三方代管 API key 有要求，商用前须走官方合作通道。
- **合规变现路径：交易所 Broker / 联盟计划**——用户通过我方 broker ID 下单或通过推荐链接注册，交易所按手续费返佣，不需要接触用户资金：
  - Binance Link/Broker：https://www.binance.com/en/link  · 联盟：https://www.binance.com/en/activity/affiliate
  - OKX Broker：https://www.okx.com/broker/home  · 下单带 `tag`/brokerCode 参数（见 V5 文档）
  - Bybit Broker：https://www.bybit.com/en/broker/  · 联盟：https://affiliates.bybit.com/
  - Bitget Broker：https://www.bitget.com/broker  · 联盟：https://partner.bitget.com/
  - 注意：ccxt 默认会在部分交易所订单里带 ccxt 自己的 broker id，接入自有 broker 时需在 `options` 中覆盖（如 `options['broker']`），否则返佣归属错误。
- 对外开放前至少需要：隐私政策（key 存储说明）、风险揭示、用户协议、key 泄露应急流程、第三方安全审计。

---

## 9. 需站长决策的问题

1. 首批接哪几家？（建议先 Binance + OKX，与现有行情一致；Bybit 第二批；Bitget 视需求）
2. 产品范围：仅现货，还是含 USDT 永续合约？最大杠杆？
3. 交易 key 存放：站长本机 executor（推荐）还是服务器 KMS？选 KMS 则选哪家云。
4. 服务器出口 IP 是否固定（白名单前提）？Cloudflare 后面的源站出口 IP 需确认。
5. 是否开子账户专门用于自动交易，起步资金与 CEX LiveLimits 具体数值。
6. 是否计划对外开放（多用户只读/交易），以及目标地区——决定是否需要法律评估。
7. 是否申请 Broker/联盟计划作为变现方式。

---

## 10. 实施状态（2026-10-13，第一期）

站长决定：对所有用户开放（先白名单内测），第一期 Binance + OKX **现货**，用户自带 key。
- 后端 `apps/api/app/exchange/`（crypto 信封加密 / store SQLite `exchange.sqlite` / connector ccxt / service 规则），路由 `app/routes/exchange.py`。
- 用户接口 `/api/v1/exchange/*`：config、accounts、`{ex}/bind`（POST 验证+绑定，DELETE 解绑删除）、balances、orders/open、orders/history、orders（下单）、orders/cancel。
- 管理接口 `/api/v1/admin/exchange`（GET/PUT：enabled 默认 false、allowlistOnly 默认 true、killSwitch、maxOrderUsdt 默认 50）、`/allowlist`（加/移用户、单用户上限）、`/audit`。
- 绑定校验：Binance `GET /sapi/v1/account/apiRestrictions`（enableWithdrawals / enableInternalTransfer 为真即拒绝；ipRestrict 必须为真）；OKX `GET /api/v5/account/config`（perm 含 withdraw 拒绝；ip 必须非空）。
- 主密钥 `AUU_EXCHANGE_MASTER_KEY`（32 字节 urlsafe base64）只在 `/etc/auu/auu.env`；每条凭证随机 DEK，AES-256-GCM，AAD 绑定 user+exchange。
- 幂等：`clientOrderId` = `auu` + 客户端 8–28 位字母数字；同一 id 不会重发；超时状态为 unknown，重试时按 clientOrderId 去交易所查询。
- 限频：每用户每分钟 30 次读、10 次写（进程内）；ccxt `enableRateLimit`。
- 与 paper、`app/live`（liveEnabled=false）完全独立，策略不接入。standby 节点拒绝 `/api/v1/exchange/` 写操作。
- 前端：`/exchange`（用户页 + 使用指南），`/admin/exchange`（管理）。
- 未做：WebSocket 推送、定时对账、合约、Bybit/Bitget、KMS 托管主密钥、交易所侧 cancel-all-after。

## 11. 第二期：USDT 本位永续（2026-10-13）
- Binance USD-M（ccxt defaultType=future）、OKX SWAP（defaultType=swap），合约符号 `BTC/USDT:USDT`。
- 接口 `/api/v1/exchange/futures/ack`、`/{ex}/futures/{positions,orders/open,funding,leverage,orders,orders/cancel,close}`；管理 `/api/v1/admin/exchange/close-all`。
- 首次风险确认必需；杠杆 ≤ maxLeverage（默认 5，可调 1–20）；合约单笔名义上限 maxFuturesUsdt（默认 50，与现货分开，可按用户覆盖），只限制开仓；紧急停止阻止开仓，reduceOnly 平仓/撤单仍可用。
- TP/SL：开仓成交请求后，另下 reduceOnly 条件单（ccxt takeProfitPrice / stopLossPrice）。
- 绑定：接受合约权限（Binance enableFutures；OKX trade），仍拒绝提现 / 内部划转 / 万向划转。
- store 改为每线程一个 SQLite 连接（修复并发读取报错）。
