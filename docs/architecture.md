# 架构速览

```mermaid
flowchart LR
  subgraph web [apps/web React+Vite]
    MarketPage --> HttpWsProvider
    CandleChart --> LWC[lightweight-charts Apache-2.0]
    Overlay[signals+fills markers]
    RiskTagBar
  end
  subgraph api [apps/api FastAPI]
    REST["/api/v1/*"]
    WS["/api/v1/ws"]
    Mock[MockMarketDataProvider]
    Pump[PumpfunPaperProvider]
    Disc[discovery new_token]
    Demo[demo-momentum-v0 signals]
    Paper[PaperBroker]
  end
  HttpWsProvider -->|REST+WS| REST
  HttpWsProvider --> WS
  REST --> Mock
  REST --> Pump
  WS --> Mock
  WS --> Pump
  Disc -->|watchlist only| Pump
  Disc -->|WS new_token| WS
  Mock --> Demo
  Pump --> Demo
```

- **默认** `DATA_PROVIDER=mock`：确定性 RNG 蜡烛 + 周期信号/成交/风控。
- **`DATA_PROVIDER=pumpfun_paper`**：本地 Pump.fun bonding-curve 模拟（venue=Pump.fun，paper-only）。`PUMPFUN_WATCH_MINTS` 白名单播种；只读发现 `PUMPFUN_DISCOVERY=pumpportal|logs|off` 可将 `new_token` 写入自选（不等于入场）。无钱包、无 `sendTransaction`。进度条绑 `progress_bps` + `complete`/`migrated`。
- **下单**：始终 `PaperBroker` + `RiskGate`（`dataSource=mock|paper` 与行情源正交）。有 `ctx.pump` 时 `estimated_impact_bps` 走 bonding-curve（buy/`buy_tokens_out` vs sell/`sell_sol_out`），否则 CEX 平方根。
- **禁止**：GPL fork（Freqtrade/FreqUI）、真实密钥、实盘下单、Jito tip / sniper、AGPL Yellowstone 嵌库。
- **后续**：`ccxt_public` / `dexscreener` provider（仅 public）。
