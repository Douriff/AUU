# pumpfun_live_paper（真实链上纸面）

`DATA_PROVIDER=pumpfun_live_paper` 把纸面行情换成链上观察到的真实成交和储备。`liveEnabled` 仍为 false：不签名、不发送交易、不读取私钥。

## 行情
- 价格 = 最近一笔真实成交之后的 `virtual_sol / virtual_token`。没有新成交，价格就不动：不插值、不造单、没有 RNG。
- 数据源（`LIVE_PAPER_FEED=auto|portal|logs|off`，默认 `auto`）：
  - PumpPortal `subscribeTokenTrade`（`wss://pumpportal.fun/api/data`）。**按消息计费**（PumpPortal 文档：每 10000 条 0.01 SOL，从 key 绑定的钱包扣），所以 `auto` 不优先用它。配置了 `PUMPFUN_PORTAL_API_KEY` 时先带 key；key 被拒（400/403）就改用不带 key 的公共数据 API。新入观察列表的 mint 会补订阅，移出的会退订。`vSolInBondingCurve` 按 SOL、`vTokensInBondingCurve` 按 UI 数量（6 位小数）换算成原始单位。
  - Solana `logsSubscribe`（`SOLANA_RPC_URL`）解码 pump 程序 `TradeEvent`（含真实储备和手续费 bps）。发现层已经在跑 `logs` 时，由发现层直接转发 TradeEvent，不再多开一条 RPC WS。
  - `auto`：优先用免费的 logs（发现层在跑 `logs` 就用它转发的，否则自己订阅）；logs 出错后 10 分钟内改用 Portal，发现层的 logs 恢复就立即断开 Portal。`portal` 强制只用 Portal。
  - 同一笔链上成交可能从 Portal 和 logs 各来一次：按 `(signature, mint, side)` 去重（health `liveFeed.duplicatesDropped`）。一笔交易里有多个 TradeEvent 时逐个应用。
  - `off` 或旧开关 `AUU_LIVE_PAPER_FEED=off`：不启动行情任务。
- 成交时间戳是本机收到的时间，和策略决策用的是同一个时钟；链上 `blockTime` 另存为 `chain_ts`。

## 只接受 pump 程序创建、未毕业的曲线
- 发现层（`discovery_accepts`）会剔除 `DemoMint*`、`complete/graduated`，以及 pool 不是 pump 的币（raydium、bonk 等）。
- provider 对每个非 `Create` 事件来源的 mint（Portal 发现、`PUMPFUN_WATCH_MINTS`、手动登记）做只读的 `getAccountInfo(["bonding-curve", mint] PDA)` 校验：账户必须存在、owner 是 pump 程序、`complete=false`。校验通过前不可交易，也没有快照。校验失败的 mint 会被移除并记住，之后不再登记。RPC 不可达时保持待定并退避重试，不会放行。首个价格用账户里的真实储备。RPC 地址取 `LIVE_PAPER_RPC_URL`，没有就用 `SOLANA_RPC_URL`。
- 从 pump `Create` 日志解码出的 mint，本身就是 pump 程序创建的，直接可交易。
- 一笔 `pump-amm`（或其他 venue）的成交，或者 `real_token_reserves=0`，都会把曲线标成 `complete`。

## 纸面成交（主窗）
- 真实行情下，入场或出场决策会先挂起。决策时刻加上 `PAPER_FILL_LATENCY_MS`（默认 300ms）之后，本机收到的**第一笔真实成交**就是成交基础：订单打在这笔成交之后的真实储备上。
  - 没有这样的成交就不成交。入场挂单超过 `LIVE_PAPER_ENTRY_TTL_MS`（默认 30000）还没等到成交就作废；出场会一直等下一笔真实成交。
- 价格 = bonding-curve 报价（`buy_tokens_out` / `sell_sol_out`），加上协议费和创作者费。费率优先用最近一笔 TradeEvent 上观察到的 bps（协议费、创作者费分别看）。没观察到（例如只有 PumpPortal 数据，它不带费率）时按保守值 **95 bps 协议费 + 30 bps 创作者费（共 125）**；`PAPER_CURVE_PROTOCOL_FEE_BPS` / `PAPER_CURVE_CREATOR_FEE_BPS` 只能往上调，不能低于这个值。
- 成交价不含费，费单独记在 `fee`：买入 `price*qty + fee` = 花掉的 SOL，卖出 `price*qty - fee` = 收到的 SOL。账本只扣一次费。
- 卖出一律按**实际持有的代币数量**全部平掉（`flatten_qty`），不再用名义金额 ÷ 价格反推数量。
- 孤儿出场按最后一个真实报价成交，不再用 `max(当前价, 入场价)` 托底。
- round8b 策略参数与 `evaluate()` 决策逻辑没有改动。

## 影子对比的成交时机
- 真实行情下影子组也用同一规则：`evaluate()` 给出的虚拟入场/出场先挂起，决策时刻 + `PAPER_FILL_LATENCY_MS` 之后的第一笔真实成交的储备就是成交价，费率同主窗。挂起期间该组该币不再评估；入场超过 `LIVE_PAPER_ENTRY_TTL_MS` 没成交就作废（决策记录 `no_real_print`）。挂起的影子单同样保护曲线不被移出观察列表。合成行情下行为不变（立即按当时快照成交）。

## 数据源标签与统计
- 每笔 Fill、每个持仓 lot、每笔已平仓交易都带 `market_source`：`real`、`synthetic`（pumpfun_paper）、`mock`，或者磁盘上旧的未标注行 `legacy_synthetic`。
- `synthetic`、`mock`、`legacy_synthetic` 和 `phantom_short` 都不进入以下统计：`/stats/paper-performance`、executability 的 Go/No-Go、影子对比列、排行榜、盘面 board、控制台当日统计。所以真实行情的统计窗口（`market_window=real`）从 0 开始。
- 旧文件原样保留：读入时把缺标签的行视为 `legacy_synthetic`，写回时不补这个键。real 成交不会和 legacy/synthetic 的 lot 对冲。

## 账本：残量空单
- 卖出如果超过持有的多头，多出的部分会被截掉（`clamped_residual_qty` 记在 fill 里），不会再开出残量空单。`pump-paper-v1` 的卖出在没有持仓时也不会开空。

## 旧数据清理（默认 dry-run）
```
cd apps/api
python -m app.paper.legacy_cleanup                         # 只报告
python -m app.paper.legacy_cleanup --apply                 # 先停 API；写前生成 .bak-<时间戳>
python -m app.paper.legacy_cleanup --journal PATH --shadow PATH
```
`--apply` 会做这些：
- 删除 `pump-paper-v1` 的残量空头 lot。
- 所有缺 `market_source` 的行标成 `legacy_synthetic`。
- `pump-paper-v1` 的 short 平仓行加 `phantom_short: true`。
- 影子对比的旧行同样标成 `legacy_synthetic`。

`closed` 里的行一条都不删。

## 影子持仓保护
观察列表淘汰 mint 时，`has_open_exposure` 同时检查纸面、实盘、策略引擎和**影子**持仓，两个 provider 都一样。仍有影子持仓的曲线不会被删掉后重新初始化到进度 0，也就不会再出现那几笔 -62% 的假止损。
