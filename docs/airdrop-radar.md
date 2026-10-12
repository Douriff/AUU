# 空投雷达（Airdrop Radar）

站内资讯栏目：汇总**市场公开空投 / 积分 / 社区奖励相关新闻**，便于用户跨项目发现机会。人工精选 seed 仅作冷启动；线上通过公开 RSS 关键词持续入库。

## 范围

- **仅资讯**：列表 + 详情。不涉及积分领取、抽奖、充值、余额、交易或提现。
- 登录可见（与「行业动态」相同鉴权）。
- 文案中性，不承诺收益或空投结果。
- 分类：`major` / `community` / `meme`；来源类型：`curated`（精选）/ `ingested`（资讯）。

## API

| Method | Path | Query |
|--------|------|--------|
| GET | `/api/v1/airdrops` | `category=all\|major\|community\|meme`，可选 `status=`、`origin=curated\|ingested`，`page` / `size` |
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
