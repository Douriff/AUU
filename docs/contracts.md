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
6. 可选帧 `type:"new_token"` → `NewTokenEvent` `{ mint, creator, slot?, initial_reserves, ts, source }`（`source=pumpportal|logs`；只读发现，不发单）

## SymbolInfo
`{ symbol, base, quote, kind, mint? }` — `pumpfun_paper` 时 `kind="pumpfun_curve"`。

## PumpCtx（可选 `StrategyContext.pump`）
`{ curve_progress_bps, virtual_sol_reserves, virtual_token_reserves, real_sol_reserves, real_token_reserves, creator_fee_bps, protocol_fee_bps?, fee_bps?, complete, migrated, amm_pool? }`  
储备字段为十进制字符串（防 JS 精度丢失）。`complete` / `migrated` 只作风控闸（进度高或毕业时 impact ×1.5），**不**写入恒定乘积公式。

## 曲线冲击（纸面 `LiquidityCtx.estimated_impact_bps`）
有 `ctx.pump`（或 `LiquidityCtx` 上同等储备字段）且虚拟储备 >0 时，走 `pumpfun_curve_math`，**不**走 CEX 平方根：

- buy：`sol_after_buy_fee` + `buy_tokens_out`（费后 SOL 进曲线）
- sell：`sell_sol_out`（token 进、SOL 出；与 buy 分叉，不得共用一支）
- `impact_bps = max(avg_px vs mid0, |mid1 − mid0| / mid0) × 1e4 + fee_bps / 2`
- 默认 `fee_bps = 125`（可经 `fee_bps` / `PumpCtx.fee_bps` / `LiquidityCtx.fee_bps` 覆盖）

无泵字段时保持 CEX：`spread_bps/2 + 40 × (notional/adv)^0.6`。

## PumpfunPaperSnapshot
`{ mint, symbol, phase:"curve"|"graduating"|"amm", progress_bps, complete, migrated, virtual_* / real_*_reserves, token_total_supply, price_sol, price_sol_str?, market_cap_sol?, creator_fee_bps, pool?, slot?, updated_ts, synthetic? }`

## PumpfunTradeTick
`{ mint, symbol, ts, side:"buy"|"sell", price, qty, sol_amount, signature?, phase:"curve"|"amm" }` — 投影到现有 `TradeTick`（`side` + 可选 `phase`）。

## NewTokenEvent（WS `type:"new_token"`）
`{ mint, creator, slot?, initial_reserves, ts, source }` — `source` ∈ `pumpportal|logs`。  
发现模块把 mint 写入 `pumpfun_paper` 自选（`discovered` 标签）；**不等于入场**，仍走 `pump-paper-v1` 的 progress / 动能 / 冲击门。`PUMPFUN_DISCOVERY=pumpportal|logs|off`；`PUMPFUN_PORTAL_API_KEY` 仅 env，永不入库。无 sniper、无下单。

REST：`GET /api/v1/pumpfun/snapshot?symbol=`（无曲线快照时 404）。  
`GET /api/v1/pumpfun/monitor` 自选盘面行。发现：`PUMPFUN_DISCOVERY` + `PUMPFUN_PORTAL_API_KEY`（仅 env）。
