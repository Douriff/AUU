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
可追加纸面证据：`quote_price?` · `estimated_impact_bps?` · `shadow_slippage_bps?`（可执行性；非链上 send）。

## RiskOut
`{ allow, clipped_size?, tags[], notes? }`

`allow=false` 时不画对应 Fill；`tags` → RiskTagBar。

## RiskEvent（WS）
`{ strategyId?, symbol?, t, risk: RiskOut }`

## REST 包络
`{ ok: true, data }` / `{ ok: false, error: { code, message } }`  
Header：`X-Api-Version: 1`

## WS
1. 服务端首帧 `{ type:"hello", version:1, providers:["mock","pumpfun_paper"], orderMode:"paper", venue }`
2. 客户端 `{ type:"subscribe", channel, symbol, interval? }`
3. channel ∈ `candles|book|trades|signals|fills|risk`
4. 心跳 `ping`/`pong` 每 15s
5. 可选帧 `type:"pumpfun_curve"` → `PumpfunPaperSnapshot`
6. 可选帧 `type:"new_token"` → `NewTokenEvent` `{ mint, creator, slot?, initial_reserves, ts, source }`（`source=pumpportal|logs`；只读发现，不发单）

Hub WS event types（非 subscribe channel）：`signal | risk | fill | reject | trading_state`  
`allow=false` / reject **永不** 伪造 Fill。

## Paper path（additive · 不改冻结字段名）

REST：

- `POST /api/v1/risk/pre-order` → `RiskOut`
- `POST /api/v1/paper/orders` → `{ fills, reject? }`（`risk.allow` 必须为 true）
- `POST /api/v1/risk/post-fill`
- `POST /api/v1/pipeline/decide-and-fill` — 一枪：provider mid/book + optional `PumpCtx` → signal → RiskGate → PaperBroker
- `GET /api/v1/live/status` — live adapter (default `liveEnabled=false`; 403 `LIVE_DISABLED` on `/live/orders` unless keypair mounted + confirm + enabled + `LiveLimits`). Health: `keypairMounted` bool + `pubkey` only; never a secret.
- `GET /api/v1/live/ledger` — `source=live` only; paper stats win-rate never mixes these fills
- `GET /api/v1/book?symbol=` — synth 深度快照（供 UI 组 ctx）
- `GET/PUT /api/v1/strategy/pump-paper-v1` — `auto_paper_orders` / `strategy_autopaper`（默认 false，无需重启）。纸面进程默认（paper round 4 Go 窗）：`progress_bps [1200,6500]`、`take_profit_pct=0.10`、`stop_loss_pct=0.07`、`max_hold_sec=300`、`max_impact_bps=75`（含费 >80 硬拒）、`max_notional_sol=0.12`。纸面开仓另过强 tape：`min_trade_count_1m=10`、`min_buy_sell_notional_ratio=2.5`，不足则 `WEAK_TAPE`（出场不用；不改 Go 门）。`liveEnabled` 默认 false；实盘名义硬顶 1.0 SOL。
- `GET /api/v1/strategy/pump-paper-v1/stats` — PaperTradeJournal 自算胜率 / 期望 / 回撤（不依赖 QuantStats）；`?mc=1` 才跑 trades-MC（默认关）
- `POST /api/v1/strategy/pump-paper-v1/stats/reset` — 清 session journal
- `GET /api/v1/stats/paper-performance` — 同上别名
- `GET /api/v1/stats/executability` — 纸面成交 vs 曲线报价的可执行性证据（`verdict=go|no-go`）；`liveEnabled` 恒 false。合同：`docs/research/executability-go-nogo-v0.md` · `docs/adapters/decision-log-v0.md` · `docs/viz/executability-panel-v0.md`
- `GET /api/v1/strategy/pump-paper-v1/decision-log?from=&to=` — `DecisionLog` 行（`reject_bucket=progress|impact|risk|none`）

## 可执行性（additive · 纸面证据，非实盘）

`ExecutabilityReport`：`lamp` · `nogo_reason` · `n_closed` / `sample_ok`（≥30）· `expectancy` · `median_entry_impact_gross_bps`（含费）· `median_entry_impact_net_bps`（扣费；go 中位 <60）· `protocol_fee_bps`（曲线地板 62.5 / AMM 10）· 含费硬顶 80 · `reject_rate.{progress,impact,risk}`（DecisionLog 聚合）· `shadow_slippage.{p50_bps,p90_bps}` · `impact_error.{p50_bps,p90_bps}` · `liveEnabled=false` · `live_checks` 三勾只读（与 live-ui-gates 同源，本栈不置 true）。无私钥、无 send。`median_entry_impact_bps` 仍是含费中位。`gates.median_entry_impact.n` 按已平仓逐笔计（journal 优先）。`gates.median_entry_impact.ok` 为 false **只**在扣费中位 ≥60，或任一含费样本 >80（等于 80 仍过；`fails_only_on=net_median>=60 OR any_gross>80`）。短样本是 `coverage_ok=false`：总 `verdict` 仍要入场冲击条数 ≥30 才可能 go，灯为灰，但这不是 60/80 失败。纸面入场在含费冲击 >80 时直接拒单；默认 `max_impact_bps=75` 是硬顶下的缓冲。`liveEnabled` 仍为 false。

`RoundTrip` 追加开仓冲击：`entry_estimated_impact_gross_bps` · `entry_protocol_fee_bps` · `entry_estimated_impact_net_bps`（Fill 上为 `estimated_impact_gross_bps` / `protocol_fee_bps` / `estimated_impact_net_bps`）。

`DecisionLogRow`：`ts, strategy_id, symbol, mint?, stage, signal_*, risk_*, notional_sol?, decision_px? / arrival_px?, impact_bps_est? / estimated_impact_bps?, impact_gross_bps?, protocol_fee_bps?, impact_net_bps?, phase?, paper_fill_px? / fill_px?, shadow_fill_px?, shadow_slippage_bps?, impact_error_bps?, shadow_source?, outcome, reject_bucket`。P0 replay=`next_trade|next_open`。不改冻结 Signal/Risk/Fill 事件名。IS 本 v0 不做。

dataSource：`mock | paper | pumpfun_paper`（`paper` / `pumpfun_paper` overlay 只订 PaperBroker Fill）。

## Venue（additive）

`venue=Pump.fun` when `DATA_PROVIDER=pumpfun_paper`（Solana bonding curve）。符号为 `PUMPDEMO/SOL` 等，报价 SOL。无私钥、无 sniper。

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
Health：`discovery` / `discoveryActive` / `discoveryReason`（Portal WS **400/403** → `portal_auth_rejected`；见 `docs/adapters/pumpportal-discovery-v0.md`）。`liveEnabled` 默认 false。

REST：

- `GET /api/v1/pumpfun/snapshot?symbol=`（无曲线快照时 404）
- `GET /api/v1/curve?symbol=` — UI 友好别名（mock 返回空曲线字段）
- `GET /api/v1/pumpfun/monitor` 自选盘面行。发现：`PUMPFUN_DISCOVERY` + `PUMPFUN_PORTAL_API_KEY`（仅 env）。

## Trader Watch → Habit → Distill（additive · 纸面观察，非镜像）

字段名冻结见 `docs/adapters/trader-watch-distill-v0.md`。数据源合同见 `docs/research/trader-learning-datasources.md`（P0 = 用户自选钱包 + Helius/RPC parsed Pump ix + 本仓 `ctx.pump`；**不**刮 frontend-api / Photon / BullX / GMGN）。Tracker/Bitquery 后置、需 env Key。

- `TraderWatchlistItem` `{ watch_id, address, label?, enabled, source:portal|rpc|indexer, added_ts, tags_override[], risk_notes? }` — 无私钥
- `TraderSnapshot` `{ watch_id, address, asof_ts, slot?, positions[{mint,symbol?,qty,cost_basis_sol?,unrealized_pnl_sol?,hold_sec,progress_bps?,phase}], open_count, gross_exposure_sol, recent_buys/recent_sells[TradeBrief], buy_notional_1h, sell_notional_1h, trade_count_1h, median_hold_sec_24h, flip_rate_24h, progress_hist, entry_progress_median_bps }`
- `TradeBrief` `{ ts, mint, side:buy|sell, sol_amount, progress_bps?, signature? }`
- `HabitTag` `{ tag:sniper|mid_curve|graduation_chase|flip|bag, confidence, evidence[] }`；同义 `curve_mid`→`mid_curve`，`quick_flip`→`flip`
- `HabitProfile` `{ watch_id, address, asof_ts, tags, primary, features }`
- `DistillResult` `{ source_watch_id, asof_ts, suggested_params:PumpPaperParamsPatch, feature_weights{progress,momentum,impact}, enabled_tags, reject_reason?, paper_compare? }`
- `CompareReport` `{ window, self:PaperStats, trader_ref, note:"reference_only — not copy-trading" }` — 胜率仍只吃自有纸面 Journal

REST：

- `GET/PUT/DELETE /api/v1/watch/traders` — 观察地址 CRUD
- `GET /api/v1/watch/traders/{id}/snapshot|habits|compare`
- `POST /api/v1/watch/traders/{id}/distill` — **只计算**
- `POST /api/v1/strategy/pump-paper-v1/apply-distill` — 须 `confirm=true`；写入纸面 params；**永不**改 `auto_paper_orders`
- `copy_trade_enabled=false` 硬编码；无 mirror 路径。`sniper` / `graduation_chase` 不自动放宽入场窗。
- `HeliusTraderReader` 仅 `TRADER_WATCH_READER=helius|rpc`（默认 mock）；live HTTP 另需 `TRADER_WATCH_LIVE_FETCH=1` + Key；不刮前端。
