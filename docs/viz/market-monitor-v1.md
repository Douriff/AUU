# 盘面监控 v1（Pump.fun / AUU）

对齐策略：`docs/strategies/pump-paper-v1.md`

## 布局

1. **左：新币/自选表** — symbol、progress_bps、1m 买卖名义、impact 预估、状态标签（curve/graduating/migrated/`discovered`）
2. **中：K 线 + Overlay** — 策略 long/flat + Fill；`CurveProgressBar`
3. **右：Tape + 风险条** — 成交流；`RiskOut.tags` / reject
4. **顶栏** — `trading_state`、dataProvider、`auto_paper_orders` / `strategy_autopaper` 开关（默认关）
5. **成功概率条** — `GET /api/v1/stats/paper-performance`：胜率、笔数、期望、回撤、蒙特卡洛区间（无平仓为空态；纸面模拟非承诺）

只读发现：WS `type:"new_token"` `{ mint, creator, slot?, initial_reserves, ts, source }` 写入 `pumpfun_paper` 自选；发现 ≠ 入场。

## 数据源

- `DATA_PROVIDER=pumpfun_paper`（仿真）→ 后续可换只读链上
- 订单路径始终 `paper` 直到用户授权实盘

## 路由

- `/` 行情监控（本规格）
- `/trade` 纸面下单（PR#2）
- `/strategy` 参数与开关
