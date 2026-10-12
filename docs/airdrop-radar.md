# 空投雷达（Airdrop Radar）

站内资讯栏目：整理**大项目空投**与**社区空投**相关公开信息，每条卡片展示 **来源（source_name）**，详情页提供摘要与「查看原文」外链。

## 范围

- **仅资讯**：列表 + 详情。不涉及积分领取、抽奖、充值、余额、交易或提现。
- 登录可见（与「行业动态」相同的鉴权门控）。
- 文案保持中性，不承诺收益或空投结果。

## API

| Method | Path | Query |
|--------|------|--------|
| GET | `/api/v1/airdrops` | `category=all\|major\|community`，可选 `status=active\|upcoming\|ended`，`page` / `size` |
| GET | `/api/v1/airdrops/{id}` | — |

响应字段含 `sourceName` / `sourceUrl` / `externalUrl` 等；前端路由 `/airdrops`、`/airdrops/:id`。

## 数据与更新

- 权威数据源是仓库内 curated 文件：`apps/api/app/airdrops/seed.json`。
- 进程首次打开 store 时写入本地 `airdrops.sqlite`（`AUU_DATA_DIR`）；seed 指纹变化时自动 upsert。
- **如何增补**：编辑 `seed.json`（必填 `id/title/project_name/category/source_name/source_url/summary/status/published_at`），重启 API 或删除 sqlite 后重新加载即可。
- 每条必须有可核对的公开 `source_url`（DefiLlama、官方博客、信誉媒体等）。禁止把禁止抓取站点的内容批量刮入仓库；本模块 **不** 做上游抓取，也 **不** 存放 API key。
- 可选：若日后要接免费 RSS，应复用 `app.news` 的拉取/失败隔离模式，且仍保留人工来源标注。

## 开关

- `AUU_AIRDROPS=off` 可在元数据中标记关闭（P0 列表仍可读已有 seed；不跑后台任务）。

## 自测

```bash
cd apps/api && .venv/bin/python -m unittest tests.test_airdrops -v
# 登录后访问 GET /api/v1/airdrops 与 /api/v1/airdrops/<id>
# 前端 /airdrops：筛选「全部 / 大项目 / 社区」，点卡片进详情，「查看原文」新标签打开且 rel=noopener
```
