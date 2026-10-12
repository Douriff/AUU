# Points trading & withdrawal

Default **off**. Paper / simulation P&L never touches the points ledger. H2 shadow hashes are untouched.

## Flags (server env)

| Variable | Default | Meaning |
|----------|---------|---------|
| `AUU_POINTS_TRADING` | off | Manual points trading (market/limit/TP-SL). Capital = available points. |
| `AUU_POINTS_TRADING_ADMIN_ONLY` | off | Soft launch: admins only. |
| `AUU_WITHDRAW_ENABLED` | off | User may apply to withdraw. |
| `AUU_WITHDRAW_ADMIN_ONLY` | off | Soft launch: admins only. |
| `AUU_TRC20_RATE` | 100 | Points per 1 USDT (shared with TRC20 top-up). |
| `AUU_WITHDRAW_MIN` / `MAX` / `DAILY_MAX` / `MAX_PENDING` | 10 / 1000 / 3000 / 3 | USDT limits (Beijing day for daily). |

Private keys are **never** stored or requested. Admin pays USDT-TRC20 from TokenPocket, pastes the tx hash; the API verifies the official USDT Transfer to the user’s address.

## Admin test enable

```bash
# on the API host (.env), then restart API (avoid 07:30–08:45 Beijing deploy window)
AUU_POINTS_TRADING=on
AUU_POINTS_TRADING_ADMIN_ONLY=on
AUU_WITHDRAW_ENABLED=on
AUU_WITHDRAW_ADMIN_ONLY=on
# optional: AUU_WITHDRAW_SKIP_CHAIN=on  # tests only — skips on-chain verify when completing
```

UI: trade panel switch **纸面/模拟** vs **积分交易**; Points page withdrawal form; `/admin/points` → withdrawals + points-trading P&L + flag readout.

## Data files

- `points.sqlite` — ledger (new kinds) + withdraw_requests (+ existing TRC20 tables)
- `points_trading.sqlite` — positions/orders for points trading only
- `mainstream_paper.sqlite` — unchanged paper account
