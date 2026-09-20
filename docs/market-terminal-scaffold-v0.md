# 可视化 · 行情终端脚手架规格 v0

状态：规格，供云端脚手架落地。范围：**行情终端 + Mock 数据源 + K 线叠加**。纸面/模拟；无密钥、无实盘下单。

对齐：OSS 初稿 v1 · StrategyDecision 冻结字段 · 栈 React+Vite+TS + Lightweight Charts + FastAPI + SQLite。

---

## 1. 目标与非目标

**目标**
- 本地 `docker compose up` / `pnpm dev` + `uvicorn` 可跑通一屏：自选列表 + K 线 + 深度摘要 + 信号/成交叠加。
- `MarketDataProvider` 可切换：`mock`（默认）| `ccxt_public`（后续）| `dexscreener`（后续）。
- Overlay 消费冻结合同：`StrategyDecision.signal`、`Fill`；告警条消费 `RiskOut.tags`。

**非目标（v0）**
- 真实下单、密钥、FreqUI/HB Dashboard fork。
- 完整订单票 / 回测报告页（仅预留路由壳）。
- GPL 前端代码移植。

---

## 2. 目录建议

```
apps/web/                 # React + Vite + TS
  src/
    app/                  # router, shell
    pages/MarketPage/
    components/
      chart/              # CandleChart, OverlayLayer
      market/             # SymbolList, DepthPanel, TradesTape
      alerts/             # RiskTagBar
    providers/            # MarketDataProvider, StrategyFeed
    types/contracts.ts    # 冻结字段 TS 镜像
    mocks/                # 确定性 mock 流
apps/api/                 # FastAPI
  app/
    main.py
    routes/{symbols,candles,signals,fills,ws}.py
    providers/{base,mock}.py
    models/
docker-compose.yml
.env.example              # 无真实密钥；DATA_PROVIDER=mock
```

---

## 3. 页面信息架构（MarketPage）

| 区域 | 组件 | 数据 |
|------|------|------|
| 左栏 | SymbolList / Watchlist | `GET /api/v1/symbols` |
| 主区 | CandleChart (LWC) | candles REST + WS |
| 主区叠加 | OverlayLayer | signals + fills |
| 右上 | DepthPanel（简版） | book 快照 WS |
| 右下 | TradesTape | trades WS |
| 顶栏 | WsStatus + RiskTagBar | hello + 最近 RiskOut.tags |

路由：`/` → MarketPage；其余 `/strategy` `/trade` `/backtest` `/alerts` `/settings` 先空壳。

---

## 4. 前端组件契约

### 4.1 CandleChart
- 库：`lightweight-charts`（Apache-2.0）。
- series：candlestick；可选 volume histogram。
- 不直接拉网；由 `useCandles(symbol, interval)` 喂 `setData` / `update`。

### 4.2 OverlayLayer
映射规则（冻结字段）：

| 来源 | 图上表现 |
|------|----------|
| `SignalOut.side=long` | 下方箭头 / buy marker，颜色 success |
| `SignalOut.side=short` | 上方箭头 / sell marker |
| `SignalOut.side=flat` | 圆点或忽略（可配） |
| `SignalOut.strength` | marker size 或透明度（0–1 → 0.4–1.0） |
| `SignalOut.reason` | hover tooltip |
| `Fill` | 小菱形，旁注 `price`/`qty`；`slippage_bps` 仅 tooltip |
| `RiskOut.allow=false` | **不**画成交；`tags` 推 RiskTagBar |

`StrategyDecision.debug`：侧栏只读 JSON，不进 series。

### 4.3 MarketDataProvider（前端）
```ts
interface MarketDataProvider {
  listSymbols(): Promise<SymbolInfo[]>
  getCandles(q: CandleQuery): Promise<Candle[]>
  subscribe(channels: SubReq[], handlers: Handlers): Unsubscribe
}
```
实现：`HttpWsProvider`（读 `VITE_API_BASE`）；开发可用 `MockBrowserProvider` 直连前端 mock（可选，默认走 API mock）。

---

## 5. 后端 API / WS（与 IA 合同一致）

### REST
- `GET /api/v1/symbols` → `SymbolInfo[]`
- `GET /api/v1/candles?symbol=&interval=&from=&to=` → `Candle[]`
- `GET /api/v1/signals?symbol=&from=&to=` → 展平后的 `SignalOut` 时间序列（可带 `strategyId`）
- `GET /api/v1/fills?symbol=&from=&to=` → `Fill[]`
- `GET /api/v1/health`

包络：`{ ok: true, data }` / `{ ok: false, error: { code, message } }`  
Header：`X-Api-Version: 1`

### WebSocket `/api/v1/ws`
1. 服务端首帧：`{ type: "hello", version: 1, providers: ["mock"] }`
2. 客户端：`{ type: "subscribe", channel: "candles"|"book"|"trades"|"signals"|"fills"|"risk", symbol, interval? }`
3. 推送：`{ type: "candle"|"trade"|"book"|"signal"|"fill"|"risk", payload }`
4. 心跳：`ping`/`pong` 每 15s

### Candle / 叠加 payload（TS 镜像）
```ts
Candle { symbol, interval, t, o, h, l, c, v }

// 冻结
SignalOut { side: "long"|"short"|"flat", strength: number, reason: string, expire_ts?: number, tags?: string[] }
RiskOut   { allow: boolean, clipped_size?: number, tags: string[], notes?: string }
Fill      { ts: number, price: number, qty: number, fee?: number, slippage_bps?: number, tag?: string }

// WS signal 帧可附 strategyId、t（锚定 K 线时间）
SignalEvent { strategyId: string, symbol: string, t: number, signal: SignalOut }
RiskEvent   { strategyId?: string, symbol?: string, t: number, risk: RiskOut }
```

---

## 6. Mock 数据源规格

`DATA_PROVIDER=mock`（默认）：

| 流 | 行为 |
|----|------|
| symbols | 固定 3–5 个伪模因对，如 `MOCK/USDC`、`PEPEMOCK/SOL` |
| candles | 确定性 RNG（seed=symbol+interval）；1s 推进一根或 update 最后一根 |
| book | 对称深度 10 档，spread 随 seed 抖动 |
| trades | 泊松到达，价格贴 mid |
| signals | 每 N 根 bar 发 long/short，带 strength/reason；偶发 flat |
| fills | 信号后 1–3 个 tick 出 Fill（模拟纸面延迟） |
| risk | 每 M 次随机 `allow=false` + tags 如 `SPREAD_TOO_WIDE`/`COOLDOWN` |

要求：同 seed 重放结果一致，便于截图/联调。

---

## 7. 实现切片（给云端编码）

**P0（本脚手架必须）**
1. Vite React TS 壳 + AppShell + MarketPage 布局
2. FastAPI mock provider + candles REST + WS hello/subscribe/candle
3. LWC K 线接 REST 历史 + WS 增量
4. OverlayLayer：mock signals + fills markers
5. RiskTagBar 吃 risk 帧
6. `.env.example` + README 本地启动三步

**P1（同仓可随后）**
- DepthPanel / TradesTape
- `ccxt_public` provider 骨架（只 public，无 key）
- Settings 页切换 `DATA_PROVIDER`

**P2**
- StrategyFeed 接真实 StrategyDecision 流
- 与 PaperBroker Fill 同源回放

---

## 8. UX 借鉴边界（合规）

- **抄**：Lightweight Charts API 用法、自研布局密度。
- **只参考**：FreqUI / HB Dashboard 的信息架构（栏位分区），不复制代码（GPL/不必 fork）。
- 产品与代码归属自有平台。

---

## 9. 验收标准

- [ ] 冷启动后 30s 内 MarketPage 出 K 线
- [ ] Mock 下可见至少 1 个 signal marker 与 1 个 fill marker
- [ ] 一次 `allow=false` 的 risk 出现在顶栏 tags，且无对应 fill 误画
- [ ] 切换 symbol 不串数据；断线重连后 hello + 恢复订阅
- [ ] 仓库无 `.env` 密钥；文档写明纸面默认

---

## 10. 开放给队友

- @策略工程：若 `SignalOut.side` 与下单 `OrderIntent.side` 枚举需统一 `buy/sell` vs `long/short`，请定一种；前端按冻结 `long|short|flat` 画图。
- @开源调研：LWC markers 目录无需再挖；Jesse/Nautilus 表到后只影响 P2 StrategyFeed。

版本：v0。后续只加字段不改名。
