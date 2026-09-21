# Venue: pump.fun（纸面）

主交易平台 = **Pump.fun**（Solana bonding curve），不是通用 CEX。

## 本轮范围

- paper / mock 优先
- mock mint 符号（`Pmp…`，报价 SOL）
- 曲线仿真：`virtual_sol_reserves`、`virtual_token_reserves`、`curve_progress`、`graduated` / `migrated`
- `MarketDataProvider` 槽位 `PumpFunPaperProvider`（`DATA_PROVIDER=pumpfun_paper`）
- 前端 `dataSource=pumpfun_paper` 占位（overlay 与 paper 相同；ctx.meta 带曲线字段）

## 明确不做

- 钱包 / 私钥 / seed
- 自动买币 sniper / copy-trade / 真 tx
- 实盘 Pump.fun 或 Solana RPC 密钥

后续若接只读公开曲线账户，仍走 paper Fill，不在本 provider 里下单。
