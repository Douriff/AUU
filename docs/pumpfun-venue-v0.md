# Venue: Pump.fun（纸面）

主交易平台 = **Pump.fun**（Solana bonding curve），不是通用 CEX。

## 本轮范围

- paper / mock 优先
- `DATA_PROVIDER=pumpfun_paper`：本地 bonding-curve 模拟（PUMPDEMO / MOONMOCK / GRADMOCK，报价 SOL）
- 曲线：`virtual_*` / `real_*` reserves、`progress_bps`、`complete` / `migrated`
- `MarketDataProvider` 槽位 `PumpfunPaperProvider`
- 前端 `dataSource=mock | paper | pumpfun_paper`（`paper` / `pumpfun_paper` overlay 与 PaperBroker Fill 对齐）
- Trade 纸面单 + `POST /api/v1/pipeline/decide-and-fill`（ctx 带 `PumpCtx` 时走曲线冲击）

详见 `docs/pumpfun-integration-v0.md`、`docs/strategies/pump-paper-v1.md`。

## 明确不做

- 钱包 / 私钥 / seed
- 自动买币 sniper / copy-trade / 真 tx
- 实盘 Pump.fun 或 Solana RPC 密钥 / Jito tip

后续若接只读公开曲线账户，仍走 paper Fill，不在本 provider 里下单。
