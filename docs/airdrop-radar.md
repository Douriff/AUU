# 空投雷达（Airdrop Radar）

站内资讯栏目：汇总**市场公开空投 / 积分 / 社区奖励相关新闻**，便于用户跨项目发现机会。人工精选 seed 仅作冷启动；线上通过公开 RSS / 交易所公开公告接口关键词持续入库（v2：官方公告层 + 去重/质量规则，见文末）。

## 范围

- **仅资讯**：列表 + 详情。不涉及积分领取、抽奖、充值、余额、交易或提现。
- 登录可见（与「行业动态」相同鉴权）。
- 文案中性，不承诺收益或空投结果。
- 分类：`major` / `community` / `meme`；来源类型：`curated`（精选）/ `ingested`（资讯）。

## API

| Method | Path | Query |
|--------|------|--------|
| GET | `/api/v1/airdrops` | `category=all\|major\|community\|meme`，可选 `status=`、`origin=curated\|ingested`、`verification=confirmed\|unverified`，`page` / `size` |
| GET | `/api/v1/airdrops/{id}` | — |

每条含 `sourceName` / `sourceUrl` / `origin`；前端 `/airdrops`。

## 数据

### 精选 seed

- 文件：`apps/api/app/airdrops/seed.json`
- 首次打开 store 时 upsert；指纹变化时更新。**不会**删除已入库的 RSS 资讯行。

### RSS 入库（持续）

- 模块：`apps/api/app/airdrops/ingest.py`
- 复用 `app.news` 的 `http_fetch` / `parse_feed`（只取标题+摘要+原文链接，不抓正文 HTML）。
- 默认源（与行业动态相同的公开 RSS，官方出口）：
  - CoinDesk `https://www.coindesk.com/arc/outboundfeeds/rss`
  - Cointelegraph `https://cointelegraph.com/rss`
- 关键词过滤（标题/摘要）：`airdrop` / `空投` / `stakedrop` / `retrodrop` / `points program` / `holder rewards` / `TGE` / `claim portal` 等。
- 分类启发式：模因关键词 → `meme`；主流协议/ETF 等 → `major`；否则 `community`。
- 入库字段：`origin=ingested`，`tags` 含 `ingested`，`source_url`=文章 URL；按 id + `source_url` 去重；**不覆盖**精选行。
- 后台：`AUU_AIRDROPS_INGEST`（默认跟随 `AUU_AIRDROPS`，测试下默认关），间隔 `AUU_AIRDROPS_INTERVAL_SEC`（默认 **240**，夹在 60–3600；实时性改造前为 900/300–3600，详见文末「实时性」）。
- 可选 `AUU_AIRDROPS_FEEDS=coindesk,cointelegraph` 限制源。
- 单源失败只记 `fetch_log`，不影响列表 API。

### 如何加源

1. 确认目标站提供**公开 RSS/Atom**且允许聚合（禁止抓取 HTML / 需密钥的接口不要加）。
2. 在 `FEED_SOURCES` 增加 `id → {name, home, url}`。
3. 用现有关键词或扩展 `AIRDROP_KEYWORDS`；补测试夹具。
4. 部署后看 `/api/v1/airdrops` 的 `feeds[]` 与 `lastIngestAt`。

## 开关

- `AUU_AIRDROPS=off` — 功能元数据关闭（已有数据仍可读）。
- `AUU_AIRDROPS_INGEST=off` — 停后台拉取。

## 自测

```bash
cd apps/api && .venv/bin/python -m unittest tests.test_airdrops tests.test_airdrops_ingest -v
# 登录后 GET /api/v1/airdrops?origin=ingested
# UI：模因 / 资讯 筛选；卡片显示来源与「资讯」徽章
```

## v2：官方公告层 + 去重与质量规则（第一阶段）

### 来源（只用免费公开来源；只取标题、≤220 字摘要、原文链接，不转载全文）

| id | 来源 | 接入方式 | 层级 | 条款 / robots 核对（2026-10-15） |
|----|------|----------|------|-----------------------------------|
| `okx` | OKX 公告 | 官方文档化公开接口 `GET https://www.okx.com/api/v5/support/announcements`（免密钥，按 IP 限频 5 次/2 秒；我们约每 3–4 分钟 1 次，见「实时性」） | official → 已确认 | robots.txt 的 `*` 组未禁止 `/api/`；API Agreement §9.4 限制的是 *Market Data* 的转售/高频抓取（公告不在其定义内），我们低频、仅标题+链接+来源标注；**条款灰区，见 PR 说明** |
| `bybit` | Bybit 公告 | 官方文档化公开接口 `GET https://api.bybit.com/v5/announcements/index`（免密钥；约每 3–4 分钟 1 次，见「实时性」） | official → 已确认 | `api.bybit.com/robots.txt` 404（无限制）；`www.bybit.com` robots 不涉及该接口；API Terms 未区分免密钥调用方（既未授权也未禁止），禁止转售/商业化 Service Data——我们不转售、仅链接回原文；**条款灰区，见 PR 说明** |
| `ethereum` `solana` `arbitrum` `optimism` `eigen` `lido` `sui` `layerzero` `kraken` | 项目/交易所官方博客 RSS | 站点自身公布的 RSS/Atom（`<link rel="alternate">` 或 Mirror/Medium 标准 feed），约每 4 分钟 1 次（Medium 16 分钟），见「实时性」 | official → 已确认 | 各站 robots.txt 均允许 feed 路径；仅取 RSS 中标题/摘要/链接；链接域名必须在白名单内（防 feed 注入外链） |
| `coindesk` `cointelegraph` | 媒体 RSS | 既有（`app.news`） | media | 沿用既有接入；单源 → 未核实 |
| **Binance** | **未接入** | 官方公告接口是需要 API Key + 签名的 WebSocket（`com_announcement_en`）；免密钥的 `bapi/.../cms/article/list/query` 被 `robots.txt`（`Disallow: */bapi/`）禁止，站点页面受 JS 挑战保护 | — | 按「不能抓取就不抓」处理；拿到官方 API Key 且用户批准后再接 |

没有 RSS / 返回 HTML / 不可达而未接入的：Chainlink、Polygon、Starknet、zkSync、Uniswap、Jupiter、LayerZero 官网、Coinbase（403）、Scroll、Linea、Aave、Ethena、Pendle。

### 质量规则（`apps/api/app/airdrops/quality.py`）

- **黑名单**：标题/摘要含「保证收益 / 保证盈利 / 稳赚 / 零风险 / 先转账 / 先打款 / send … first / guaranteed / risk-free / double your」等字样的条目一律不收录（抓取时丢弃；启动迁移会清掉库中已有命中行；seed 命中也会被丢弃）。
- **同一活动合并**（时间窗 14 天）：URL 相同（去 utm 等跟踪参数）；或标题词集 Jaccard ≥ 0.6；或「共享项目/代币实体 + 同一事件类型（airdrop / launch / points / claim）」且来自不同发布方。同一发布方的两条只有 URL 或标题高度相同才合并。
- **核实标签**：`confirmed`（已确认）= 含官方来源，或 ≥ 2 个独立发布方；其余 `unverified`（未核实）。精选 seed 默认只有 1 个来源 → 未核实（除非 seed 行写 `"official": true`）。
- **固定风险提示**：每张卡片、详情页、API 响应（`risk` / `riskNotice`）都带。
- 数据：`sightings` 表保存每个来源的一条记录；`items.verification / source_count / official` 为聚合结果；旧库启动时自动迁移（回滚旧代码安全：新增列/表有默认值，旧代码忽略）。

### 刷新、降级与备用机

- 沿用 `run_loop`（实时性改造后默认 240 秒一轮）；每源独立最短间隔（见「实时性」），失败后指数退避（×2…×16，上限 4 小时），日志 `airdrops ingest <src> failed (old rows kept, backing off): <原因>`，旧数据保留，`feeds[]` 状态点变红；单源失败不影响其它源和站点。
- `AUU_ROLE=standby`：`ingest_enabled()` 恒为 False，`tick()` 也直接返回；`airdrops_ingest` 列入 `role.STANDBY_OFF`（`/health` 的 `jobsOff`）。
- `AUU_AIRDROPS_FEEDS=okx,bybit,...` 可按源关停（例如条款复核后关掉某个交易所）。

### 测试

```bash
cd apps/api && AUU_TEST=1 python -m unittest tests.test_airdrops tests.test_airdrops_ingest tests.test_airdrops_links tests.test_airdrops_v2 tests.test_standby_role
```

## 实时性（本站数据要尽快更新）

目标：新公告 / 新头条出现后，站内数据与页面在几分钟内更新，且不超过各来源允许的请求频率。

### 后端抓取

| 项 | 改前 | 改后 | 环境变量 |
|---|---|---|---|
| 后台轮询周期 | 900 s | **240 s**（夹在 60–3600） | `AUU_AIRDROPS_INTERVAL_SEC` |
| OKX / Bybit 公告 API 每源最短间隔 | 1800 s | **180 s** | `AUU_AIRDROPS_MIN_API_SEC` |
| 官方博客 RSS（含 Paragraph/Optimism） | 1800 s | **240 s** | `AUU_AIRDROPS_MIN_RSS_SEC` |
| 媒体 RSS（CoinDesk / Cointelegraph） | 0（每轮） | **240 s** | `AUU_AIRDROPS_MIN_MEDIA_SEC` |
| LayerZero（Medium，`Cache-Control: private, max-age=900`） | 1800 s | **960 s**（4×RSS，`slow: 4`） | 随 RSS 变量 |
| 任何来源硬下限 | — | **60 s**（环境变量设得再小也不会低于它） | `HARD_MIN_INTERVAL` |
| 失败退避 | ×2…×16，上限 4 h | **不变**（首次失败至少等 300 s） | — |
| `Retry-After`（429/503） | 无 | 遵守（上限 4 h，与退避取较大者） | — |

- 实际节奏 = 轮询周期的整数倍：OKX/Bybit/博客/媒体每 ~4 分钟 1 次（每轮 1 个请求），Medium 约每 16 分钟 1 次。每个源约 15 个请求/小时（改前约 2/小时；媒体改前 4/小时），13 个源合计约 180 个请求/小时（改前约 30），其中带校验头的源大多返回 304（几百字节）。
- OKX 官方限频 5 次/2 秒、Bybit 600 次/5 秒（按 IP），我们是 1 次/4 分钟，低 3 个数量级。**注意：OKX / Bybit 条款灰区（见上文 v2 表）在频率上比 v2 高了约 7 倍，复核条款后可用 `AUU_AIRDROPS_FEEDS` 去掉任一交易所，或用 `AUU_AIRDROPS_MIN_API_SEC` 调慢。**
- **条件请求**：每次请求带 `User-Agent: AUUTRADE-airdrops/2.0 (+https://auutrade.com; headlines + links only, low frequency)`、`Accept-Encoding: gzip`；该源上次成功响应有 `ETag` / `Last-Modified` 时发 `If-None-Match` / `If-Modified-Since`（存在 `airdrops.sqlite` 的 `fetch_validators` 表，重启后仍有效；新表，旧代码忽略，回滚安全）。收到 304 不解析、不入库，只刷新该源的「最近成功时间」（`lastIngestAt`）。解析失败时不保存校验头。2026-10-18 实测各源响应头：Ethereum / Arbitrum / Eigen / Lido / Kraken / Paragraph / Cointelegraph 有 ETag 或 Last-Modified，所有被测源都支持 gzip；OKX / Bybit 公告 API 无校验头（照常整包返回：OKX ≈5 KB、gzip ≈1.4 KB；Bybit ≈25 KB、gzip ≈6 KB）。
- 响应体上限 3 MB（压缩前与解压后都限制），不带 cookie、不带凭证。
- 备用机：`AUU_ROLE=standby` 时 `ingest_enabled()` 恒为 False，`tick()` 直接返回，不发任何请求（有测试）。

### Optimism feed 的 HTTP 308

`https://optimism.mirror.xyz/feed/atom` 已被 Mirror 重定向（301 → `paragraph.com/api/blogs/rss/@the-optimism-collective` → **308** → `api.paragraph.com/blogs/rss/@the-optimism-collective`），老版本 urllib 不跟随 308，源一直显示 `http 308`。处理：

1. 源地址改为 Paragraph 公布的 `https://paragraph.com/api/blogs/rss/@the-optimism-collective`（其 robots.txt 明确 `Allow: /api/blogs/rss/`），链接白名单加入 `paragraph.com`（保留 `mirror.xyz` / `optimism.io`，旧数据不受影响）。
2. 抓取器自己跟随 301/302/303/307/308（最多 5 跳；仅 http(s)；https 不得降级到 http；每一跳都带同样的 UA、不带 cookie），所以以后再换地址也不会因 3xx 失败。
3. 测试：`tests/test_airdrops_realtime.py` 含本地 HTTP 源的 301→308→200 链、重定向环、缺 Location、降级拒绝，以及「旧 URL 经重定向链 → 入库 → 已确认 → 第二轮 304」的端到端用例。

> 注意：`api.paragraph.com/robots.txt` 是 `Disallow: /`（API 域名对所有爬虫关闭），而 `paragraph.com` 的 robots 明确放行了该 RSS 路径并 308 到它。我们取的是站点自己公布的 feed，频率 1 次/4 分钟且为条件请求；若要更保守，可把 `optimism` 从 `AUU_AIRDROPS_FEEDS` 去掉。

### 前端（`/airdrops`）

- 每 **60 秒**自动刷新（`AUTO_REFRESH_MS`）；页面不可见（`document.hidden`）时暂停，重新可见时**立即刷新**并重新计时；刷新是后台静默的（不闪骨架屏、不丢当前列表/筛选/页码），切换筛选时较旧的响应会被丢弃（不会覆盖新结果）。
- 页头显示「更新于 x 分钟前 · 来源最近抓取 y 分钟前」（每 15 秒重算；「来源最近抓取」用接口返回的 `serverNow`，不受本机时钟偏差影响）。
- 温和提示（黄色提示条，不影响页面其它部分）：某些来源最近一次抓取失败（显示来源名，说明仍展示之前保存的内容、系统自动重试）；自动刷新请求失败（说明显示的是 x 分钟前的内容）；全部来源超过 max(15 分钟, 3 个抓取周期) 没有成功更新。
- 「行业动态」页：后台默认周期 720 s → **300 s**（`AUU_NEWS_INTERVAL_SEC`，下限仍 300），页面轮询 120 s → 60 s（原本就有可见性暂停）。
- API：`GET /api/v1/airdrops` 新增 `serverNow`，`feeds[].minIntervalSec`；`ingestIntervalSec` 默认变为 240。
- 纯函数与定时器逻辑在 `apps/web/src/pages/AirdropsPage/refresh.ts`，单测：`cd apps/web && npm run test:airdrop`。

### 端到端延迟实测

**方法**（`ops/measure/`，全部只在本机，用本地 mock 源，不碰任何真实服务/服务器/库/密钥）：

- `airdrop_latency_harness.py mock-origin`：本地 HTTP 源扮演 OKX（JSON，无校验头）、CoinDesk（媒体 RSS）、Lido（官方博客 RSS）、Optimism（走 301 → 308 → 200 的重定向链），RSS 支持 `ETag` / 304；按计划在随机时刻各发布一条新条目。
- `airdrop_latency_harness.py api`：运行**真实的** FastAPI + `run_loop` + 真实 HTTP 抓取，仅把四个源 URL 指到 mock；**不设**任何间隔环境变量，即测的是默认值（240 s 周期、180/240 s 间隔）。
- `airdrop_latency_browser.py`：无头 Chrome 打开 `/airdrops`（真实 60 s 自动刷新，不手动刷新），记录每条的 ① 接口首次返回时间（同一会话每 2 秒轮询）② 标题首次出现在页面 DOM 的时间（MutationObserver）。时钟统一用 `CLOCK_MONOTONIC`（沙箱 VM 暂停会让墙钟跳变，第一次用墙钟的试跑因此作废并重做；本次浏览器/Python 单调时钟偏移漂移 −0.4 ms）。
- `airdrop_latency_harness.py sim`：用真实的 `AirdropFetcher._due` / tick 逻辑在假时钟上回放 2 天，对比改前（900 s 周期、1800 s 间隔、媒体 0）与改后（默认），7 个源 × 400 个随机发布时刻（模拟，不是实测）。
- `airdrop_ui_check.py`：浏览器内数自动刷新请求并截图。

**结果**（2026-10-18，16 条，每源 4 条，一次 ~26 分钟；原始数据 `docs/measure/airdrop-latency-2026-10-18.json`）：

| 指标（发布 → …） | 平均 | 中位 | P90 | 最大 |
|---|---|---|---|---|
| 接口可见（后端已入库并能从 `/api/v1/airdrops` 读到） | **129 s** | 144 s | 212 s | 225 s |
| 页面上可见（不手动刷新，含前端 60 s 轮询） | **166 s** | 181 s | 249 s | **261 s（4.4 分钟）** |

- 理论上界：一个周期 240 s + 抓取耗时 + 页面轮询 60 s ≈ 5 分钟；理论平均 ≈ 120 s + 30 s ≈ 2.5 分钟。实测与之吻合（接口最大 225 s < 243 s；页面最大 261 s < 5 分钟）。
- 这次页面比接口多出的时间几乎恒为 ≈37 s：240 s 周期恰是 60 s 轮询的 4 倍，两者相位锁定了；一般情况下这一项在 0–60 s 之间均匀分布（平均 30 s）。
- 四类源（OKX / 媒体 / 官方博客 / 经 308 重定向的 Optimism）都在一个周期内被读到；RSS 源在无新内容的轮次返回 304。
- 前端行为（`airdrop_ui_check.py`）：可见时 125 s 内 2 次请求，间隔 60.0 s；标签页隐藏 130 s 内 **0** 次请求；重新可见后 3 s 内立即 1 次请求。页头显示「更新于 刚刚 · 来源最近抓取 x 分钟前」（英文：`Updated just now · sources last fetched x minutes ago`）。
- 回放对比（模拟，7 个源）：改前平均 **790 s（13 分钟）**、P90 1557 s、最大 1806 s（30 分钟），且旧页面没有自动刷新；改后平均 173 s、P90 318 s；除 Medium（LayerZero，960 s 间隔）外最大 243 s。
- **局限**：mock 源没有「源站自己的延迟」。真实源还要加上它们自己的发布与 CDN 缓存时间（实测响应头：Cointelegraph `s-maxage=300`、Kraken `max-age=300`、Lido `max-age=600`、Paragraph CDN 约 5 分钟、Medium `max-age=900`），这部分我们无法也不应绕过。没有对真实源做只读实测。


### 回滚

全部为代码默认值，近似回滚到 v2 节奏只需环境变量（Medium 会变成 2 h，其余与 v2 相同）：`AUU_AIRDROPS_INTERVAL_SEC=900 AUU_AIRDROPS_MIN_API_SEC=1800 AUU_AIRDROPS_MIN_RSS_SEC=1800 AUU_AIRDROPS_MIN_MEDIA_SEC=900`、`AUU_NEWS_INTERVAL_SEC=720`（前端轮询需发版）。

### 测试

```bash
cd apps/api && AUU_TEST=1 python -m unittest tests.test_airdrops tests.test_airdrops_ingest tests.test_airdrops_links tests.test_airdrops_v2 tests.test_airdrops_realtime tests.test_standby_role tests.test_news
cd apps/web && npm run build && npm run test:airdrop
```
