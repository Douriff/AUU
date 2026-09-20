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
