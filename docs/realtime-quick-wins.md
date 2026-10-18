# 行情实时性快速改善（第 0 步，WebSocket 之前）

范围：只改行情读取/缓存/轮询节奏、价格提醒节奏、同步与备份清单。不碰交易、积分、提现、密钥逻辑；`liveEnabled=false` 不变；
备用机 `AUU_ROLE=standby` 行为不变（价格提醒检查仍只在主服运行）。

## 改动

| # | 内容 | 开关 / 默认 |
|---|---|---|
| 1 | 币安 REST 域名可配置 + 自动回退（只影响无密钥的公共行情 ccxt 客户端；OKX 回退不变） | `AUU_BINANCE_REST_HOSTS`，默认 `api1,api3,data-api.binance.vision,api.binance.com`；standby 默认 `api.binance.com` 在前 |
| 2 | 全站共用一份 ticker 缓存，TTL 3 秒（下限 2 秒）；前端 markets 轮询 30→5 秒 | `AUU_MAINSTREAM_TICKER_SEC`（默认 3，范围 2–600） |
| 3 | 1h/4h/1d 最后一根：有人看图时每 10 秒补拉「前一根+当前根」（1 次请求），只放内存，不写 sqlite | `AUU_MAINSTREAM_TAIL_SEC`（默认 10，范围 2–300） |
| 4 | 价格提醒检查 15→5 秒；实时价新鲜度上限 10 分钟→15 秒（缓存不可用回退到本地库行时仍按旧的 10 分钟规则）；一次性触发的条件更新、邮件限流（每 15 秒最多 5 封）不变 | `MAX_PRICE_AGE_MS`、`CHECK_INTERVAL_SEC` |
| 5 | 页头价与图表最后一根同一价格来源（图表最后一根的收盘/最高/最低用同一份缓存价覆盖，不写库） | 接口多返回 `priceAt` / `tailAt` |
| 6 | 香港同步清单（DBS）和备份清单加入 `price_alerts.sqlite`、`chart_drawings.sqlite`、`airdrops.sqlite`；`exchange.sqlite`（用户交易所密钥）按设计不同步、不备份 | 见 `ops/sync/README.md`（服务器脚本的参考副本，合并本 PR 不会改服务器） |

## 交易所请求量（与用户数无关）

- ticker：全站最多每 3 秒 1 次 `ticker/24hr?symbols=…`（19 币），**权重 2**（东京实测响应头 `x-mbx-used-weight-1m` 增量）→ 40 权重/分钟。
- 最后一根补拉：每个（币×周期）最多每 10 秒 1 次 `klines?limit=2`，**权重 2**，且只在有人看那张图时；只对币池内的币有效（19 币：1h/1d 各 19 + 4h 3 = 41 个序列）→ 理论上限 41×6×2 ≈ 490 权重/分钟。
- 1m/5m/15m 按需补拉的节流不变（每序列 10 秒 1 次，19×3 个序列理论上限 ≈ 680 权重/分钟，改动前就存在）。
- 币安上限 6000 权重/分钟/IP；上述全部同时打满约 1200，再加 `cex.py`/`tickers.py` 的大盘看板（`ticker/24hr` 全量，权重 80，TTL 8 秒 → 最多约 600）合计约 1900（约 32%）。遇 418/429 退避 2 分钟，不换域名（权重按 IP 计）。

## 东京实测（复制行情库，不写线上库）

方法：`ops/measure/realtime_latency_harness.py`，在东京服务器以 `auu` 用户、`nice 10` 运行；启动时用只读备份接口复制 `mainstream.sqlite`，其余数据都写在 `/tmp/rt1/<label>/`；
邮件通道关闭；同一时段并行跑「改前（origin/main，页面 30 秒、K 线 60 秒轮询）」和「改后（本分支，页面 5 秒、K 线 10 秒轮询）」各 960 秒（前 100 秒预热不计），
真值是同机每秒取一次币安 `ticker/price`、每次轮询取 `klines`；价格提醒每 15 秒创建 3 个距真值 0.5–8 bp 的 BTC 穿越提醒。年龄 = 屏幕上那个数据被服务器取到后经过的时间。结果见 PR 描述。
