# pumpfun_live_paper（真实链上纸面）

`DATA_PROVIDER=pumpfun_live_paper` 把纸面行情换成链上观察到的成交和储备。`liveEnabled` 仍为 false：不签名、不发送交易、不读取私钥。

- 价格 = 最近一笔真实成交的 `virtual_sol / virtual_token`。没有新成交时价格保持不变，不插值、不造单。
- 优先 PumpPortal WebSocket `subscribeTokenTrade`（`vSolInBondingCurve` / `vTokensInBondingCurve`）。连接失败时退到 Solana `logsSubscribe`，用 `decode_trade_event` 解 pump 程序 TradeEvent。
- `AUU_LIVE_PAPER_FEED=off` 时不打开只读行情任务。
- 发现层只接受 pump 程序创建且 `complete=false` 的新币。`DemoMint*` 与已毕业币不进真实统计。
- `pumpfun_paper` 仍是合成行情（`synthetic=true`），只给测试和演示。这类成交标 `market_source=synthetic`，不进 Go、影子对比列、排行榜。
- 磁盘上没有 `market_source` 的旧账本在读入时标 `legacy_synthetic`，保留原文件（写出时不补这个键），不计入 Go。新窗口字段是 `market_window=real`。
- 清理脚本只报告、不改文件：`python -m app.paper.phantom_report`（`--apply` 也会拒绝写）。
