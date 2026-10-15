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
- 后台：`AUU_AIRDROPS_INGEST`（默认跟随 `AUU_AIRDROPS`，测试下默认关），间隔 `AUU_AIRDROPS_INTERVAL_SEC`（默认 900，夹在 300–3600）。
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
| `okx` | OKX 公告 | 官方文档化公开接口 `GET https://www.okx.com/api/v5/support/announcements`（免密钥，按 IP 限频 5 次/2 秒；我们每 30 分钟 1 次） | official → 已确认 | robots.txt 的 `*` 组未禁止 `/api/`；API Agreement §9.4 限制的是 *Market Data* 的转售/高频抓取（公告不在其定义内），我们低频、仅标题+链接+来源标注；**条款灰区，见 PR 说明** |
| `bybit` | Bybit 公告 | 官方文档化公开接口 `GET https://api.bybit.com/v5/announcements/index`（免密钥；每 30 分钟 1 次） | official → 已确认 | `api.bybit.com/robots.txt` 404（无限制）；`www.bybit.com` robots 不涉及该接口；API Terms 未区分免密钥调用方（既未授权也未禁止），禁止转售/商业化 Service Data——我们不转售、仅链接回原文；**条款灰区，见 PR 说明** |
| `ethereum` `solana` `arbitrum` `optimism` `eigen` `lido` `sui` `layerzero` `kraken` | 项目/交易所官方博客 RSS | 站点自身公布的 RSS/Atom（`<link rel="alternate">` 或 Mirror/Medium 标准 feed），每 30 分钟 1 次 | official → 已确认 | 各站 robots.txt 均允许 feed 路径；仅取 RSS 中标题/摘要/链接；链接域名必须在白名单内（防 feed 注入外链） |
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

- 沿用 `run_loop`（默认 900 秒一轮）；每源独立 `min_interval`（交易所/博客 1800 秒），失败后指数退避（×2…×16，上限 4 小时），日志 `airdrops ingest <src> failed (old rows kept, backing off): <原因>`，旧数据保留，`feeds[]` 状态点变红；单源失败不影响其它源和站点。
- `AUU_ROLE=standby`：`ingest_enabled()` 恒为 False，`tick()` 也直接返回；`airdrops_ingest` 列入 `role.STANDBY_OFF`（`/health` 的 `jobsOff`）。
- `AUU_AIRDROPS_FEEDS=okx,bybit,...` 可按源关停（例如条款复核后关掉某个交易所）。

### 测试

```bash
cd apps/api && AUU_TEST=1 python -m unittest tests.test_airdrops tests.test_airdrops_ingest tests.test_airdrops_links tests.test_airdrops_v2 tests.test_standby_role
```
