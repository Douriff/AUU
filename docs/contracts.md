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
1. 服务端首帧 `{ type:"hello", version:1, providers:["mock","pumpfun_paper"], orderMode:"paper" }`
2. 客户端 `{ type:"subscribe", channel, symbol, interval? }`
3. channel ∈ `candles|book|trades|signals|fills|risk`
4. 心跳 `ping`/`pong` 每 15s
5. 可选帧 `type:"pumpfun_curve"` → `PumpfunPaperSnapshot`

## SymbolInfo
`{ symbol, base, quote, kind, mint? }` — `pumpfun_paper` 时 `kind="pumpfun_curve"`。

## PumpCtx（可选 `StrategyContext.pump`）
`{ curve_progress_bps, virtual_sol_reserves, virtual_token_reserves, real_sol_reserves, real_token_reserves, creator_fee_bps, complete, migrated, amm_pool? }`  
储备字段为十进制字符串（防 JS 精度丢失）。

## PumpfunPaperSnapshot
`{ mint, symbol, phase:"curve"|"graduating"|"amm", progress_bps, complete, migrated, virtual_* / real_*_reserves, token_total_supply, price_sol, price_sol_str?, market_cap_sol?, creator_fee_bps, pool?, slot?, updated_ts, synthetic? }`

## PumpfunTradeTick
`{ mint, symbol, ts, side:"buy"|"sell", price, qty, sol_amount, signature?, phase:"curve"|"amm" }` — 投影到现有 `TradeTick`（`side` + 可选 `phase`）。

REST：`GET /api/v1/pumpfun/snapshot?symbol=`（无曲线快照时 404）。
