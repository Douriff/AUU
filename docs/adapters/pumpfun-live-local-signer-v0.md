# Pump.fun live adapter · local signer v0

Status: **scaffold, dark**. This PR does **not** send chain transactions.

Venue = Pump.fun bonding curve on Solana. No sniper, no Jito tip racing, no MEV.

## Paper vs live

| | Paper (default, working) | Live (this adapter) |
|--|--------------------------|---------------------|
| Health | `mode=paper`, `liveDisabled=true` | same default; `liveSendWired=false` |
| Path | `RiskGate.check` → `PaperBroker.submit` → Fill | `RiskGate.check_live` → `LiveBroker.submit` |
| Fill | local / simulated | **never** in this PR (stub reject `LIVE_STUB`) |
| Keys | none | filesystem path `AUU_SOLANA_KEYPAIR_PATH` only |
| Autopaper | `strategy_autopaper` default **false** | not wired; live routes 403 unless armed |

Paper is unchanged. Autopaper stays off by default. Live is a separate route family under `/api/v1/live/*`.

## Hard gate (locked caps; cannot arm without keypair)

A single **LiveDisabled** switch defaults **ON** (`AUU_LIVE_DISABLED` unset/true, plus Settings). Enabling live also requires:

1. Local keypair **file path** via gitignored env `AUU_SOLANA_KEYPAIR_PATH` — never a private-key string in repo, env value, logs, or the Settings upload (there is no upload).
2. Explicit `live_armed=true` (`AUU_LIVE_ARMED`, default **false** even if a keypair file exists).

Locked user-authorized live caps (not placeholders; Settings shows them read-only):

| Cap | Value |
|-----|-------|
| `max_notional_sol` per order | **1.0 SOL** |
| `max_day_loss_pct` | **0.045 (4.5%)** |
| `max_open_mints` concurrent | **10** |

Env may only **tighten** these; zero/unset falls back to the locked values. Missing keypair or `live_armed=false` → live routes return **403** with `error.reasons` among:

- `LIVE_DISABLED`
- `NO_KEYPAIR`

`LIVE_SEND_WIRED` is **false** in this scaffold, so `GET /api/v1/health` keeps `liveDisabled=true` even if the checklist later passes. `LiveBroker` still does not submit a transaction.

## Gitignored `.env` (local machine only)

Copy `.env.example` → `.env` (already gitignored). Do **not** commit `.env`, `id.json`, or `*keypair*.json`.

```bash
# Live adapter — leave disabled. Caps are locked in RiskGate / live config.
AUU_LIVE_DISABLED=true
AUU_LIVE_ARMED=false
AUU_SOLANA_KEYPAIR_PATH=          # e.g. /home/you/.config/solana/id.json
```

The API never logs secret bytes from the keypair file. `LocalSigner.inspect` only checks that the path exists and looks like a Solana JSON byte array, then drops the contents.

Settings shows the three locked caps and a **LIVE DISABLED / NOT ARMED** badge. **Arm live** stays off without a keypair file.

## RiskGate live extras

Mapped from vn.py-style hard gates in `docs/adapters/vnpy-riskmanager-v0.md`. Paper `RiskGate.check()` does **not** read these (paper day-loss stays 5%).

| Limit | Fail closed when |
|-------|------------------|
| `max_notional_sol` = 1.0 | order notional exceeds it (`MAX_NOTIONAL`) |
| `max_day_loss_pct` = 0.045 | `day_pnl / equity <= -4.5%` (`DAY_LOSS_BREAKER`) |
| `max_open_mints` = 10 | new open when `meta.open_mints >= 10` (`MAX_OPEN_MINTS`) |

Explicit zeros passed into `check_live` still return `LIMITS_MISSING` (no clip, no send).

## LiveBroker stub + later `@pump-fun/pump-sdk`

`POST /api/v1/live/orders` uses the same lifecycle as paper (pre-order → fill/reject events) tagged `venue=live`, then **stops**.

When a later PR wires send, call the official MIT package [`@pump-fun/pump-sdk`](https://www.npmjs.com/package/@pump-fun/pump-sdk) — **not** a sniper, **not** Jito:

```typescript
import { Connection } from "@solana/web3.js";
import {
  PumpSdk,
  getBuyTokenAmountFromSolAmount,
  getSellSolAmountFromTokenAmount,
} from "@pump-fun/pump-sdk";

const connection = new Connection(process.env.SOLANA_RPC_URL!); // public RPC; no API key in repo
const sdk = new PumpSdk(connection);

// buy
const global = await sdk.fetchGlobal();
const { bondingCurveAccountInfo, bondingCurve, associatedUserAccountInfo } =
  await sdk.fetchBuyState(mint, user);
const instructions = await sdk.buyInstructions({
  global,
  bondingCurveAccountInfo,
  bondingCurve,
  associatedUserAccountInfo,
  mint,
  user,
  solAmount, // BN lamports, already clipped by max_notional_sol
  amount: getBuyTokenAmountFromSolAmount(global, bondingCurve, solAmount),
  slippage: 1,
});

// sell
const sellState = await sdk.fetchSellState(mint, user);
const sellIxs = await sdk.sellInstructions({
  global,
  bondingCurveAccountInfo: sellState.bondingCurveAccountInfo,
  bondingCurve: sellState.bondingCurve,
  mint,
  user,
  amount,
  solAmount: getSellSolAmountFromTokenAmount(global, sellState.bondingCurve, amount),
  slippage: 1,
});

// Then: sign with LocalSigner (filesystem keypair) and submit ONLY if
// live_armed && !live_disabled && limits complete && LIVE_SEND_WIRED.
// This v0 PR does not do that.
```

Forbidden now and later: Jito tips, sniper/create-listen auto-buy, private-key strings in env, committing `id.json` / `*keypair*.json`.

## REST

- `GET /api/v1/live/status` — `liveDisabled`, `liveArmed`, `reasons`, `limits`, `keypairConfigured` (boolean only)
- `PUT /api/v1/live/limits` — placeholder numbers; zero/null does not arm
- `PUT /api/v1/live/disabled` — cannot turn the switch off without keypair + limits
- `PUT /api/v1/live/arm` — cannot arm without switch off + keypair + limits
- `POST /api/v1/live/pre-order` · `POST /api/v1/live/orders` — **403** unless armed; armed still returns `LIVE_STUB` (no Fill)

Paper: `POST /api/v1/risk/pre-order` → `POST /api/v1/paper/orders` unchanged.
