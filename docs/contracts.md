# 冻结合同字段（P0）

与 `apps/api/app/models/contracts.py`、`apps/web/src/types/contracts.ts` 保持同名。

## Candle
`{ symbol, interval, t, o, h, l, c, v }` — `t` 为 unix ms。

## SignalOut
`{ side: "long"|"short"|"flat", strength?, reason?, expire_ts?, tags? }`

图表映射：`long`→下方 buy/arrowUp；`short`→上方 sell/arrowDown；`flat` 可忽略。

## SignalEvent（WS）
`{ strategyId, symbol, t, signal: SignalOut }`

## Fill
`{ ts, price, qty, fee?, slippage_bps?, tag? }` — 图上菱形/方块 marker。

## RiskOut
`{ allow, clipped_size?, tags[], notes? }`

`allow=false` 时不画对应 Fill；`tags` → RiskTagBar。

## RiskEvent（WS）
`{ strategyId?, symbol?, t, risk: RiskOut }`

## REST 包络
`{ ok: true, data }` / `{ ok: false, error: { code, message } }`  
Header：`X-Api-Version: 1`

## WS
1. 服务端首帧 `{ type:"hello", version:1, providers:["mock"] }`
2. 客户端 `{ type:"subscribe", channel, symbol, interval? }`
3. channel ∈ `candles|book|trades|signals|fills|risk`
4. 心跳 `ping`/`pong` 每 15s

## Paper path（additive · 不改冻结字段名）

REST：

- `POST /api/v1/risk/pre-order` → `RiskOut`
- `POST /api/v1/paper/orders` → `{ fills, reject? }`（`risk.allow` 必须为 true）
- `POST /api/v1/risk/post-fill`
- `POST /api/v1/pipeline/decide-and-fill` — 一枪：mock mid/book 组 ctx → signal → RiskGate → PaperBroker
- `GET /api/v1/book?symbol=` — mock 深度快照（供 UI 组 ctx）

Hub WS event types（非 subscribe channel）：`signal | risk | fill | reject | trading_state`  
`allow=false` / reject **永不** 伪造 Fill。

## Venue（additive）

`venue=pump.fun`（Solana bonding curve）。符号为 mock mint，报价 SOL。

- `GET /api/v1/curve?symbol=` → virtual SOL/token reserves、curve_progress、graduated/migrated、price_sol
- `SymbolInfo` 加法字段：`mint, venue, curve_progress, virtual_*_reserves, graduated, migrated`（不改冻结名）
- dataSource：`mock | paper | pumpfun_paper`
- `PumpFunPaperProvider`：paper 槽位；无私钥、无 sniper

