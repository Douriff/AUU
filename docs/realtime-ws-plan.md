# 行情实时化第二阶段：交易所 WebSocket → `/api/v1/ws` 推送（设计 + 第 ①② 步实现说明）

> 前置：第 0 步已上线（PR #116，见 `docs/realtime-quick-wins.md`）：全站 ticker 缓存 3 秒、前端行情轮询 5 秒、1h/4h/1d 末根每 10 秒补齐、价格提醒 5 秒。东京实测页头价格平均延迟约 3.6 秒。
> 本文把剩余工作拆成 5 步。**本 PR 只实现第 ①② 步（后端），默认关闭**；不改前端、nginx、线上配置，不部署。

## 0. 目标与总体结构

| 指标 | 第 0 步（现在） | 本方案目标 |
|---|---|---|
| 页头价格年龄（屏幕上的数被交易所产生后经过的时间） | 平均约 3.6 秒（3 秒缓存 + 5 秒轮询） | 平均 ≤ 1 秒（币安 miniTicker 本身 1 秒一推，服务器内部 < 10 毫秒，再加 Cloudflare 往返） |
| 图表末根（1h/4h/1d） | 10 秒补拉 | ≤ 2 秒（币安 kline 2 秒一推） |
| 交易所 REST 权重 | ticker 40/分钟 + 末根补拉最多约 490/分钟 | 推流新鲜时这两项为 0，只剩交叉校验约 2/分钟 |
| 价格提醒 | 5 秒轮询 + 3 秒缓存 | 第 ① 步后即用推流价（仍 5 秒检查）；第 ④ 步改为事件触发（< 1 秒） |

```
Binance 现货 combined stream (wss://stream.binance.com:443/stream?streams=…)  ─┐   asyncio 任务，断线指数退避重连，
OKX /ws/v5/public (tickers) + /ws/v5/business (candle) 作为后备                ─┴─▶ 心跳 / 空闲看门狗 / 24h 轮换
                                                                                   │ 每帧解析 ~10 µs，不碰 sqlite、不阻塞事件循环
                                                                                   ▼
                                       MarketBus（进程内存：每交易所最新 ticker、每序列 当前根+前一根、连接新鲜度）
                                        │                                      │
             ① REST 读路径覆盖（新鲜才用，过期自动回落 REST）            ② /api/v1/ws 推送（订阅、合并、背压、限流）
             markets / candles 末根 / 价格提醒 读到的就是推流价          nginx /api/v1/ws → Cloudflare（WebSocket）→ 浏览器
                                        ▲
             REST 交叉校验（每 60 秒 1 次 ticker/24hr，偏差超阈值连续 2 次 → 该流标记可疑，读路径改回 REST）
```

为什么经 Cloudflare 走 WebSocket 而不是 SSE：已有 `/api/v1/ws`（鉴权、CSRF Origin 检查、nginx `location /api/v1/ws` 都在）；Cloudflare 各套餐都代理 WebSocket；
SSE 经 Cloudflare 有响应缓冲、100 秒无数据超时等坑，而且是单向，取消订阅要另开 HTTP 请求。

## 1. 分步计划

### ① 后端交易所 WS 行情接入层 + 断线重连 + 与 REST 交叉校验（本 PR 已实现）

内容
- `app/marketdata/realtime/feeds.py`：币安现货一个连接订阅 `<币>@miniTicker`（1 秒）+ `<币>@kline_1h/4h/1d`（2 秒），19 币 × 4 = 76 个 stream（上限 1024）；OKX 两个连接：`/public` 订阅 `tickers`，`/business` 订阅 `candle1H`、`candle4H`、`candle1Dutc`（OKX 的 `1D` 按香港时间切日，`1Dutc` 才和币安/本地库的 UTC 日线对齐）。
- 连接：443 端口；币安备用网关 `data-stream.binance.vision`（只读行情镜像），连不上或连上不出数据就换下一个。OKX 不带 `:8443`（OKX 2026-10-31 起关闭 8443）。
- 心跳：`websockets` 自动回应币安每 20 秒的协议 ping，并自发 20 秒协议 ping（发现半开 TCP）；空闲看门狗 `AUU_RT_IDLE_SEC`（默认 20 秒）无任何消息就重连；OKX 在静默时先发文本 `ping`，再静默一个周期没 `pong` 就重连；连接满 23.5 小时主动轮换（币安 24 小时强制断开）。
- 重连：指数退避 `base × 2^(n-1)`，上限 60 秒，乘 [0.5, 1) 随机抖动；连接稳定出数据 60 秒后清零；每个连接 5 分钟内最多 30 次建连（币安限制 300 次/5 分钟/IP，OKX 3 次/秒/IP）。
- 不阻塞事件循环：每帧在读循环里直接解析（小 JSON），不跨线程、不写库；积压时每 50 帧主动 `sleep(0)` 让出；交叉校验的 REST 请求放 `asyncio.to_thread`。
- 过期检测：连接级 `last_data_ms` 超过 `AUU_RT_STALE_SEC`（默认 10 秒）、连接断开、或被交叉校验标记可疑 → 视为不新鲜；重连后旧值在被新连接重新推送前不使用。
- 交叉校验：每 60 秒用一次批量 `ticker/24hr`（权重 2）对比 5 秒内收到的推流价，最大偏差超过 `AUU_RT_XCHECK_BPS`（默认 50 bp）连续 2 次 → 该交易所流标记可疑 2 个周期。
- 读路径覆盖：`MainstreamService._tickers()` 在推流新鲜且全部币都已收到时直接用推流价（不发 REST），否则走原逻辑；`_stored_tail()` 同理用推流的当前根+前一根，当前根不属于当前周期时回落 REST。价格提醒检查线程走同一入口，自动用上推流价。
- 选源：优先 REST 层正在用的交易所（`svc.active`，通常币安）；它不新鲜就用另一个新鲜的（OKX）；都不新鲜 → 无源，一切回到 REST。

验收标准
- `AUU_RT_WS` 未设置/关闭时：不建任何交易所 WS 连接，`/api/v1/ws` 握手、hello、所有回复与现在逐字节一致，`/api/v1/mainstream/realtime` 返回 `{"enabled": false}`；全量测试无新增失败。
- 打开时：mock 交易所单测覆盖解析、重连退避序列、空闲看门狗、OKX ping/pong、换网关、过期回落 REST、交叉校验标记可疑、5000 帧积压时事件循环最大停顿 ≤ 50 毫秒。
- 上线观察（第 ⑤ 步）：24 小时内 `parseErrors=0`，非计划断线 ≤ 每小时 1 次，交叉校验偏差 p99 < 20 bp。

回滚：`AUU_RT_WS=off`（或删掉该行）后重启 API，即回到第 0 步行为；无数据迁移、无库表变更。

### ② 内存行情总线 + `/api/v1/ws` 推送 ticker / 末根 K 线（本 PR 已实现）

内容
- `MarketBus`：进程内存保存最新 ticker、每序列当前根+前一根、各连接新鲜度；REST 读路径用线程锁拿快照（只锁字典拷贝，微秒级）。
- 推送协议（只增不改，旧的 paper 事件帧不变）：
  - 客户端：`{"type":"subscribe","channel":"ticker","symbols":["BTC","ETH"]}`、`{"type":"subscribe","channel":"kline","symbol":"BTC","interval":"1h"}`（1h/4h/1d）、`unsubscribe` 同格式。
  - 服务端：`subscribed` 确认 → 立即发最新快照 → 每次更新发 `ticker` / `kline` 帧（含交易所时间 `ts`/`eventTs`、服务器收到时间 `recvAt`）；源切换或推流过期时发 `rt_status {live, source}`，前端据此回落轮询。
  - hello 增加 `realtime` 字段与 `ticker/kline/rt_status` 事件类型（仅开关打开时）。
- 鉴权：沿用 `AuthGateMiddleware`（开 `AUU_AUTH` 时握手需登录 cookie，Origin 必须是本站）；网关把用户 id 放进 scope 供每账户连接上限使用。
- 限流与上限（仅开关打开时生效）：进程总连接 `AUU_RT_WS_MAX_CLIENTS`（2000）、每账户（未登录按 IP）`AUU_RT_WS_PER_CLIENT`（8，nginx 另有每 IP 10 个）、每连接订阅数 `AUU_RT_WS_MAX_SUBS`（64）、上行消息令牌桶 `AUU_RT_WS_MSG_BURST`（每 10 秒 20 条，累计违规 20 次断开 1008）、客户端空闲 `AUU_RT_WS_CLIENT_IDLE_SEC`（90 秒无任何上行帧断开；服务器每 15 秒发 ping，浏览器会回 pong）。
- 背压：每个连接一个发送任务；每个订阅键只保留最新一帧（合并，旧帧计入 `coalesced`），所以慢客户端最多积压「订阅数」帧，内存有上界，交易所读循环永远不等客户端；单帧发送超过 `AUU_RT_WS_SEND_TIMEOUT_SEC`（5 秒）→ 以 1013 断开该客户端。
- 每帧只序列化一次，所有订阅者共用同一字符串。
- 状态接口 `GET /api/v1/mainstream/realtime`：各连接新鲜度、重连次数、最近退避、交叉校验结果、客户端数、拒绝数、慢客户端断开数。

验收标准
- 端到端（mock 交易所 → API → 客户端）p99 延迟 < 50 毫秒；100 个并发客户端时 API 进程 CPU < 1 核的 20%、内存增量 < 50 MB；无错误、无慢客户端误断（本地实测见第 3 节）。
- 上限/限流/过期推送/慢客户端断开均有单测。

回滚：`AUU_RT_WS_PUSH=off`（保留 ① 的读路径覆盖，只关推送）或 `AUU_RT_WS=off`（全关）。

### ③ 前端订阅与降级回轮询（下一 PR）

内容
- `HttpWsProvider` 收到 hello 带 `realtime` 时：行情列表订阅 `ticker`（全部 19 币）、图表页订阅当前币 `ticker` + 当前周期 `kline`（1h/4h/1d；1m/5m/15m 暂仍轮询）；切币/切周期时取消旧订阅。
- 收到 `ticker` 直接更新页头价与列表行，`kline` 合并到图表最后一根（与第 0 步「页头与末根同源」一致）。
- 降级：WS 断开、`rt_status.live=false`、或 5 秒没收到任何行情帧 → 恢复现有 5 秒/10 秒轮询；恢复推送后再把轮询降到 30 秒做兜底校准。
- 重连：指数退避 + 抖动（1→30 秒），防止 Cloudflare/服务器重启后的同时重连洪峰；页面隐藏时（`visibilitychange`）断开，回到前台重连。
- 多标签页：可选 `BroadcastChannel` 共享一个连接（避免撞到每账户 8 个上限）。

验收标准：东京实测页头价格平均年龄 ≤ 1.2 秒、p95 ≤ 2 秒；断网/关推送时 10 秒内回到轮询且页面无报错；前端构建无新增失败。
回滚：前端开关（如 `VITE_RT_WS=off` 或后端 `AUU_RT_WS_PUSH=off`，前端看不到 `realtime` 字段就保持轮询）。

### ④ 价格提醒改为基于推送事件（下一 PR）

内容：价格提醒订阅总线（同进程回调，不经 WebSocket），对有活跃提醒的币每次 ticker 更新立即判定穿越（内存里缓存活跃提醒阈值，命中后才进 sqlite 线程写事件）；保留 5 秒轮询作兜底（推流不新鲜时）；邮件限流（每 15 秒 5 封）不变；standby 仍不检查不发信。
验收标准：从交易所推送到提醒事件落库 p99 < 1 秒；推流断开时轮询兜底继续工作；不重复触发。
回滚：开关 `AUU_PRICE_ALERTS_EVENTS=off` 回到 5 秒轮询。

### ⑤ nginx / Cloudflare 配置、监控与实测（上线步骤）

- nginx：`location /api/v1/ws` 已有 `Upgrade/Connection`、`proxy_read_timeout 3600s`、`limit_conn auu_conn 10`；补 `proxy_send_timeout 3600s`，确认 `proxy_buffering off`（WS 实际不缓冲，但写明）；CSP `connect-src` 已含 `wss:`。
- Cloudflare：确认 Network → WebSockets 开启；WS 空闲 100 秒会被断（服务器 15 秒 ping 已覆盖）；Cloudflare 发布/维护时会断开 WS，靠前端重连；AOP（mTLS）对 WS 握手同样生效，无需改。
- 监控：auu-guard 增加检查 `/api/v1/mainstream/realtime`：`selected` 为空或主连接 `dataAgeMs > 60000` 持续 2 分钟告警；记录每小时重连次数、交叉校验最大偏差、客户端数、慢客户端断开数。
- 灰度：先在香港 standby 打开 `AUU_RT_WS=on` 观察 24 小时（无用户流量）；再在东京打开 `AUU_RT_WS=on` + `AUU_RT_WS_PUSH=off`（只让 REST 读路径用推流价，前端不变即可受益）；第 ③ 步前端上线后再开推送。
- 实测：沿用 `ops/measure/realtime_latency_harness.py` 的方法，在东京同时段对比「第 0 步」与「推流」页头价格年龄；用 `ops/measure/realtime_ws_e2e.py` 的客户端部分经 Cloudflare 连 100 个 WS 测 CPU/内存。
- 回滚：`AUU_RT_WS=off` + 重启；nginx 改动只加超时，可原样回退。

## 2. standby（香港）节点行为

- 开关独立：每台机器各自读 `AUU_RT_WS`。推流只读交易所公共行情，不写账本、不发邮件，所以 standby 可以安全打开（建议打开，故障切换时行情已是热的）。
- 各自直连交易所，各占自己 IP 的连接/权重额度，互不影响。香港到 `api.binance.com` 实测约 73 ms，WS 同机房链路预计相当。
- 价格提醒检查仍只在主服运行（`price_alerts_enabled()` 在 standby 为假），第 ④ 步保持这一点。
- 切换主备（`AUU_ROLE=primary` 重启）不需要改推流配置。

## 3. 本地端到端实测（mock 交易所，本 PR）

方法：`ops/measure/realtime_ws_e2e.py run`（box：8 vCPU，Python 3.13，uvicorn + websockets 14.1）。mock 交易所子进程按币安节奏推送（19 币 miniTicker 每秒 1 次 + 19 币 × 3 周期 kline 每 2 秒 1 次 = 47.5 帧/秒，均匀错开），帧内 `E` = 发送时刻；
API 子进程 `AUU_RT_WS=on`（只连 mock，交叉校验与所有外联后台任务关闭）；客户端分 4 个子进程，每个客户端订阅 19 币 ticker + BTC/ETH 1h kline。延迟 = 客户端收到时刻 − `E`（同机时钟，1 毫秒分辨率），每阶段 60 秒、前 2 秒快照不计。

| 场景 | API CPU（单核 %） | RSS | 收到帧数 | 端到端延迟 p50 / p90 / p99 / max（ms） |
|---|---|---|---|---|
| 开关关闭，空闲（= 现在） | 0.2 | 75.8 MB | — | — |
| 开关打开，0 客户端 | 1.1 | 76.3 MB | — | — |
| 1 客户端 | 1.3 | 77.3 MB | 1,160 | 1 / 1 / 1 / 2 |
| **100 客户端** | **6.1** | **83.6 MB** | 115,960 | **3 / 4 / 5 / 8** |
| 压力：交易所 10 倍速率（475 帧/秒），100 客户端 | 40.9 | 83.4 MB | 758,189 | 2 / 3 / 6 / 21 |

- 交易所 → 服务器解析 p99 1 ms；其余是服务器 → 100 个客户端的扇出（p99 5 ms）。无错误、无慢客户端断开、无解析错误、交易所连接 0 次断开。
- 100 客户端内存增量约 7 MB。
- 生产延迟估算：币安 miniTicker 本身每 1 秒推一次（价格在推送前平均已「老」约 0.5 秒）+ 东京↔币安几毫秒 + 服务器 < 10 ms + Cloudflare/用户网络 RTT/2 → 页头价格年龄平均约 0.6–0.9 秒，对比第 0 步约 3.6 秒。

## 4. 交易所连接限制与我们的用量

| | 限制（官方文档，2026-10 查阅） | 本方案用量 |
|---|---|---|
| 币安现货 stream | 每连接上行 5 条/秒（ping/pong/订阅都算）；每连接 1024 stream；每 IP 每 5 分钟 300 次建连；连接最长 24 小时；服务器每 20 秒 ping，1 分钟内必须 pong | 1 个连接、76 stream、URL 里一次订阅（0 条订阅消息）、协议 ping 每 20 秒 1 条；自限每 5 分钟 ≤ 30 次建连；23.5 小时主动轮换 |
| 币安 USDⓈ-M 合约 WS | 端点已迁到 `/public`、`/market`、`/private` 路径（见之前审计结论） | 本阶段不用；以后接资金费率/标记价推送时按新路径配置，不能沿用旧 `fstream…/ws` 路径 |
| OKX | 每 IP 建连 3 次/秒；每连接订阅/退订/登录 480 次/小时；单条订阅 ≤ 64 KB；30 秒无数据断开（需发 `ping`）；8443 端口 2026-10-31 停用；K 线频道在 `/business` | 2 个连接（tickers / candle），每次建连 1 条订阅消息；静默 20 秒发 `ping`；默认 443 |

PR #120 时两家都全量订阅（OKX 热备），3 个交易所连接/节点。香港实测 OKX tickers 一路就约 36 KB/s，所以后续改成 OKX 省流待机（默认，见 4.1）：平时只有币安 1 个全量连接 + OKX `/public` 1 个只挂探测订阅的空闲连接；`AUU_RT_OKX_MODE=full` 回到 #120 行为。

### 4.1 OKX 省流待机（`AUU_RT_OKX_MODE=standby`，默认）

- 待机：OKX `/public` 连接保持（热连接，不用重新握手），只订阅 1 个探测频道 `funding-rate:BTC-USDT-SWAP`（OKX 每 30–90 秒推一条，约 0.56 KB）；静默 20 秒发文本 `ping` 收 `pong`，加上 20 秒协议 ping。这样 OKX 不会因为“无订阅/30 秒无数据”断开，也能随时知道 OKX 网关是否正常（`status.okx.standbyHealthy`）。探测帧只证明连接活着，**不算行情**，不会让 OKX 变成“新鲜”。`/business`（K 线）待机时完全不连（懒连接）。
- 唤醒（任一条件，选源循环判断；币安断线由 feed 立即唤醒选源循环，不等 1 秒节拍）：币安断线（计划内的 23.5 小时轮换除外，那时只按“过期”判断）；币安最新数据超过 `AUU_RT_OKX_WAKE_SEC`（默认 3 秒，比 10 秒过期早）；币安被交叉校验标记可疑，或出现第一次偏差（strike）；REST 层本身已切到 OKX。唤醒 = 在已连好的 `/public` 上发 1 条 `subscribe tickers`（19 币），`/business` 开始连接并订阅 K 线。
- 回落：币安连续健康 `AUU_RT_OKX_CALM_SEC`（默认 300 秒）后发 1 条 `unsubscribe tickers`，关 `/business`。期间币安只要再不健康一次，计时清零。选源本身仍然是币安一新鲜就立刻切回（和 #120 相同），OKX 只是多热一会儿，防止再次故障时又要等。
- 抖动防护：`AUU_RT_OKX_FLAP_WINDOW_SEC`（默认 1800 秒）内每多唤醒一次，冷静期翻倍（300 → 600 → 1200 …），上限 `AUU_RT_OKX_CALM_MAX_SEC`（默认 3600 秒）；窗口外的唤醒不再计数。每个 OKX 连接自限 400 次订阅/退订/小时（官方 480）：唤醒时额度不够就直接重连（新连接一次订阅全部），回落时额度不够就保持订阅（宁可多花流量也不丢数据）。
- 启动：前 `AUU_RT_OKX_BOOT_GRACE_SEC`（默认 20 秒）给币安首包时间，不唤醒 OKX；过了还没数据就唤醒。
- 只配了 OKX（`AUU_RT_WS_EXCHANGES=okx`）时永远是 full。
- 不阻塞事件循环：模式切换在单独 task 里做（一次小 `send` 或 `close`）；交叉校验仍在线程里用 REST。
- 切到 OKX 多出来的延迟：币安断线时 ≈ 1 个往返（订阅 → OKX 立即推快照）。本机到 OKX 实测 warm 订阅到 19 币全部首包 p50 102 ms / max 103 ms（本机→OKX 往返约 100 ms；香港到 OKX 走 Cloudflare 应更短）；而 #120 的 full 模式要等选源 1 秒节拍（mock 实测 p50 699 ms），所以实际上不比原来慢。币安“静默不出数据”时两种模式都在 10 秒过期时切换，OKX 已在 3 秒时唤醒，不多等。K 线：`/business` 冷连接首根 K 线本机实测 p50 532 ms（握手 433 ms），这段时间图表走原来的 REST 末根补拉。

## 5. 成本

- 交易所：公共行情免费，无需 API key。
- 入站流量（每节点）：**更正**：#120 原估算把 KB/s 换算成 GB/月算错了（12 KB/s ≈ 31 GB/月，不是 1 GB）。香港 standby 2026-10-19 实测（`ss` 每连接 bytes_received，30 秒）：币安约 7 KB/s ≈ 18 GB/月，OKX tickers 约 36 KB/s ≈ 93 GB/月，OKX candles 约 2 KB/s ≈ 5 GB/月，网卡合计约 50 KB/s ≈ 130 GB/月。
- 省流待机后：OKX 待机实测（mock + 字节计数代理，`ops/measure/okx_standby_bytes.py bytes`，探测每 30–60 秒一条）WS 层 8.7–17.4 B/s，加 TLS/TCP 头估算 18–28 B/s ≈ 46–73 MB/月；每次唤醒按 300 秒冷静期约多 11 MB（36+2 KB/s × 300 s）。预计每节点入站 ≈ 币安 18 GB/月 + OKX < 0.1 GB/月 + 唤醒次数 × 11 MB。若还要再省，大头是币安 57 个 kline stream（1h/4h/1d），可考虑只给正在看的币订 K 线。
- 出站流量：每个订阅 19 币 ticker 的页面约 19–20 帧/秒 × 约 170 B ≈ 3.4 KB/s；100 个同时在线页面 ≈ 340 KB/s ≈ 2.7 Mbps，持续在线一个月约 0.9 TB（上限估算，实际按在线时长折算）。Cloudflare 不按流量计费；VPS 出口流量额度需要核对。图表页只订阅 1 币时约 0.3 KB/s。
- CPU / 内存：见第 3 节；100 客户端约 6% 单核、+7 MB。
- 交易所 REST 权重下降：推流新鲜时 ticker（约 40/分钟）与 1h/4h/1d 末根补拉（最多约 490/分钟）归零，新增交叉校验约 2/分钟。

## 6. 开关一览（全部默认值；不设 `AUU_RT_WS` 就什么都不启动）

| 变量 | 默认 | 说明 |
|---|---|---|
| `AUU_RT_WS` | off | 总开关 |
| `AUU_RT_WS_PUSH` | on | 开总开关后是否在 `/api/v1/ws` 推送 |
| `AUU_RT_WS_OVERLAY` | on | 开总开关后 REST 读路径是否用推流价/末根 |
| `AUU_RT_WS_EXCHANGES` | `binance,okx` | 要连的交易所 |
| `AUU_RT_BINANCE_WS_URLS` | `wss://stream.binance.com:443,wss://data-stream.binance.vision:443` | 依次尝试 |
| `AUU_RT_OKX_PUBLIC_WS_URL` / `AUU_RT_OKX_BUSINESS_WS_URL` | `wss://ws.okx.com/ws/v5/public` / `…/business` | |
| `AUU_RT_STALE_SEC` | 10（3–120） | 超时视为过期，读路径回落 REST |
| `AUU_RT_IDLE_SEC` | 20 | 无消息重连（OKX 先 ping） |
| `AUU_RT_BACKOFF_BASE_SEC` / `AUU_RT_BACKOFF_MAX_SEC` | 1 / 60 | 重连退避 |
| `AUU_RT_XCHECK_SEC` / `AUU_RT_XCHECK_BPS` | 60 / 50 | 交叉校验周期 / 阈值（0 = 关） |
| `AUU_RT_OKX_MODE` | `standby` | `standby` = 省流待机（4.1）；`full` = #120 行为，OKX 一直全量订阅 |
| `AUU_RT_OKX_WAKE_SEC` | 3（1–60） | 币安数据超过这么久没更新就唤醒 OKX |
| `AUU_RT_OKX_CALM_SEC` / `AUU_RT_OKX_CALM_MAX_SEC` | 300 / 3600 | 币安连续健康多久后 OKX 回待机 / 抖动翻倍上限 |
| `AUU_RT_OKX_FLAP_WINDOW_SEC` | 1800 | 抖动计数窗口 |
| `AUU_RT_OKX_BOOT_GRACE_SEC` | 20 | 启动时给币安的首包时间 |
| `AUU_RT_OKX_PROBE` | `funding-rate:BTC-USDT-SWAP` | 待机探测订阅（`频道:instId`） |
| `AUU_RT_WS_MAX_CLIENTS` / `AUU_RT_WS_PER_CLIENT` / `AUU_RT_WS_MAX_SUBS` | 2000 / 8 / 64 | 连接与订阅上限 |
| `AUU_RT_WS_MSG_BURST` / `AUU_RT_WS_SEND_TIMEOUT_SEC` / `AUU_RT_WS_CLIENT_IDLE_SEC` | 20 / 5 / 90 | 上行限流 / 慢客户端 / 空闲 |

## 7. 风险

- 推流价与 REST 价的微小差异：miniTicker 是 1 秒聚合，REST `ticker/24hr` 是请求时刻；同一交易所，差异在一个推送周期内。交叉校验只拦大偏差（50 bp）。
- 选源切换（币安 ↔ OKX）时价格会跳一个两所价差，`rt_status.source` 会告知；REST 层本来就有同样的切换。
- 单进程内存总线：如果以后 uvicorn 改为多 worker，每个 worker 会各自连交易所（连接数 × worker 数，仍远低于限额），客户端只拿到所在 worker 的推送——功能正确但重复；那时再引入单独的行情进程或共享总线。
- Cloudflare 维护/发布、服务器重启会同时断开全部 WS，前端必须带抖动重连（第 ③ 步）。
- 1m/5m/15m 周期本阶段仍走 10 秒按需补拉，未接推流。
- 币安 miniTicker 下限 1 秒；如需更快（亚秒）可对正在看的单个币加订 `@aggTrade`/`@bookTicker`，消息量大，按需开。
