# Live UI gates v0

对齐：`docs/adapters/pumpfun-live-local-signer-v0.md` · `docs/viz/paper-stats-v1.md`

Live adapter stays **dark**. Default runtime sends **zero** chain txs. Paper autopaper / PaperTradeJournal / stats win-rate stay paper-only.

## 1. Gates (all required)

Reject with `LIVE_DISABLED` (RiskTagBar + `RiskOut.tags` + 403 `error.tags`) unless **all** of:

1. Local keypair **mounted** (`AUU_SOLANA_KEYPAIR_PATH` file present) — UI shows `mounted: yes|no` only, **no private-key field**
2. User **explicit secondary confirm** (Settings dialog → `liveConfirmed`)
3. `liveEnabled` **true** (default **false**)
4. `LiveLimits` present (locked, read-only):

| Cap | Value |
|-----|-------|
| `max_notional_sol` | **1** |
| `max_day_loss_pct` | **0.045** |
| `max_open_mints` | **10** |

`LiveLimits` is a **separate type** from paper `RiskLimits` / `pump-paper-v1` params. Do not reuse paper 5% day-loss or paper notional.

## 2. Settings layout

```
┌─ Live adapter ─────────────────────────────────┐
│ liveEnabled OFF (default)                      │
│ LIVE DISABLED  LIVE_DISABLED  NO_KEYPAIR       │  ← same tags as alert bar
│ max_notional_sol     1     (locked, read-only) │
│ max_day_loss_pct     0.045 (locked, read-only) │
│ max_open_mints       10    (locked, read-only) │
│ keypair              mounted: yes | no         │
│ [Enable live…] → confirm dialog                │
└────────────────────────────────────────────────┘
```

Confirm copy: secondary confirm required; send gate stays closed; zero chain txs.

## 3. Alert bar

Market top `RiskTagBar` and `/alerts` always surface `LIVE_DISABLED` while the four-part checklist fails. Paper `ALLOW` can coexist — the tag is the live path, not a paper deny.

Topbar badge: `LIVE OFF · LIVE_DISABLED`.

## 4. Paper vs live ledger

- Autopaper / Trade → `PaperBroker` → `PaperTradeJournal` (`source=manual|signal`)
- Paper stats `win_rate` **never** includes `source=live`
- If live fills exist later: `GET /api/v1/live/ledger` (`source=live`) only

## 5. Signing / send

- Intent build: official `@pump-fun/pump-sdk` `buyInstructions` / `sellInstructions` only
- Send gate is **outside** `liveDisabled` / `liveEnabled` (`LIVE_SEND_WIRED=false`)
- Default: unsigned intent may be described; **no** chain submit
