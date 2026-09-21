# 盘面监控 v1（Pump.fun / AUU）

对齐策略：`docs/strategies/pump-paper-v1.md`

## 布局

1. **左：新币/自选表** — symbol、progress_bps、1m 买卖名义、impact 预估、状态标签（curve/graduating/migrated）
2. **中：K 线 + Overlay** — 策略 long/flat + Fill；`CurveProgressBar`
3. **右：Tape + 风险条** — 成交流；`RiskOut.tags` / reject
4. **顶栏** — `trading_state`、dataProvider、`auto_paper_orders` 开关（默认关）

## 数据源

- `DATA_PROVIDER=pumpfun_paper`（仿真）→ 后续可换只读链上
- 订单路径始终 `paper` 直到用户授权实盘

## 路由

- `/` 行情监控（本规格）
- `/trade` 纸面下单（PR#2）
- `/strategy` 参数与开关
