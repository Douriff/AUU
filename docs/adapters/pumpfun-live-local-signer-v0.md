# Pump.fun live adapter · local signer v0

Status: **scaffold, dark**. This PR does **not** send chain transactions.

Venue = Pump.fun bonding curve on Solana. No sniper, no Jito tip racing, no MEV.

UI gates: `docs/viz/live-ui-gates-v0.md`.

## Paper vs live

| | Paper (default, working) | Live (this adapter) |
|--|--------------------------|---------------------|
| Health | `mode=paper`, `liveEnabled=false` | same default; `liveSendWired=false` |
| Path | `RiskGate.check` → `PaperBroker.submit` → Fill | `RiskGate.check_live` → `LiveBroker.submit` |
| Limits | paper `RiskLimits` (day-loss **5%**) | separate `LiveLimits` **1 / 0.045 / 10** |
| Fill | local / simulated | **never** in this PR (send gate → `LIVE_STUB`) |
| Journal | `PaperTradeJournal` `source=manual\|signal` | `source=live` ledger only; **not** in paper win-rate |
| Keys | none | LOCAL-ONLY `secrets/live-keypair.json` (gitignored) |
| Autopaper | `strategy_autopaper` default **false** | not wired; live routes 403 unless checklist |

Paper is unchanged. Autopaper stays off by default. Live is a separate route family under `/api/v1/live/*`.

## Hard gate (`liveEnabled` default false)

Reject with **`LIVE_DISABLED`** (403 `error.reasons` / `error.tags` **and** `RiskOut.tags`) unless **all** of:

1. Local keypair **mounted** at gitignored **`secrets/live-keypair.json`** (or `AUU_SOLANA_KEYPAIR_PATH`) — never a secret string in repo, env value, logs, or Settings (there is no input). Health: `keypairMounted` bool + `pubkeyShort` only.
2. User **explicit secondary confirm** (`liveConfirmed` / Settings dialog). `AUU_LIVE_ARMED` is an alias; default **false**.
3. `liveEnabled` **true**. Default **false** (`AUU_LIVE_ENABLED` unset, or `AUU_LIVE_DISABLED` true).
4. `LiveLimits` present (locked caps below).

Locked user-authorized live caps (not paper limits; Settings shows them read-only):

| Cap | Value |
|-----|-------|
| `max_notional_sol` per order | **1.0 SOL** |
| `max_day_loss_pct` | **0.045 (4.5%)** |
| `max_open_mints` concurrent | **10** |

Env may only **tighten** these; zero/unset falls back to the locked values.

`LIVE_SEND_WIRED` is **false**. The **send gate is outside** `liveDisabled` / `liveEnabled`: checklist can pass and intent can be described; default runtime still submits **zero** chain txs. `GET /api/v1/health` keeps `liveDisabled=true` while send is unwired.

## LOCAL-ONLY keypair mount

AUU does **not** generate, request, or commit key material. Put an existing Solana CLI JSON keypair on **this machine**:

```bash
mkdir -p secrets
# copy your local JSON array of 64 ints (Phantom base58 converted locally) to:
#   secrets/live-keypair.json
# optional override (gitignored .env):
AUU_LIVE_ENABLED=false
AUU_LIVE_DISABLED=true
AUU_LIVE_ARMED=false
AUU_SOLANA_KEYPAIR_PATH=secrets/live-keypair.json
```

Do **not** commit `.env`, `secrets/live-keypair.json`, `id.json`, or `*keypair*.json`.

`GET /api/v1/health` / `GET /api/v1/live/status`:

| Field | Expose |
|-------|--------|
| `keypairMounted` | **bool** (`true` when the 64-int file is present) |
| `pubkey` / `pubkeyShort` | shortened public key (`8fs58…akFi`) or `null` |
| secret / JSON array / full private key | **never** |

The API never logs secret bytes. `LocalSigner.inspect` reads the file, derives `pubkeyShort` from the public half of a 64-int Solana CLI array (last 32 bytes), then drops the contents. `liveEnabled` stays **false** until Settings secondary confirm.

Settings: `liveEnabled` default off + confirm dialog; three locked caps read-only; keypair `mounted` bool + `pubkeyShort` only. Alert bar shows `LIVE_DISABLED`.

## RiskGate live extras

Mapped from vn.py-style hard gates in `docs/adapters/vnpy-riskmanager-v0.md`. Paper `RiskGate.check()` does **not** read `LiveLimits` (paper day-loss stays 5%).

| Limit | Fail closed when |
|-------|------------------|
| `max_notional_sol` = 1.0 | order notional exceeds it (`MAX_NOTIONAL`) |
| `max_day_loss_pct` = 0.045 | `day_pnl / equity <= -4.5%` (`DAY_LOSS_BREAKER`) |
| `max_open_mints` = 10 | new open when `meta.open_mints >= 10` (`MAX_OPEN_MINTS`) |
| checklist | any of the four gates missing (`LIVE_DISABLED`, plus `NO_KEYPAIR` / `LIMITS_MISSING`) |

Explicit zeros passed into `check_live` still return `LIMITS_MISSING` (no clip, no send).

## LiveBroker stub + later `@pump-fun/pump-sdk`

`POST /api/v1/live/orders` uses the same lifecycle as paper (pre-order → fill/reject events) tagged `venue=live`, then **stops**.

Intent build: official MIT package [`@pump-fun/pump-sdk`](https://www.npmjs.com/package/@pump-fun/pump-sdk) `buyInstructions` / `sellInstructions` **only** — **not** a sniper, **not** Jito. Python describes that shape; it does not import the TS SDK.

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
// send gate (LIVE_SEND_WIRED) is open — independent of liveEnabled.
// This v0 PR does not do that.
```

Forbidden now and later: Jito tips, sniper/create-listen auto-buy, private-key strings in env, committing `id.json` / `*keypair*.json`.

## REST

- `GET /api/v1/live/status` — `liveEnabled`, `liveConfirmed`, `liveDisabled`, `liveArmed`, `reasons`, `limits`, `keypairMounted` (bool), `pubkeyShort`
- `GET /api/v1/live/ledger` — `source=live` journal (empty here; never mixed into paper win-rate)
- `PUT /api/v1/live/limits` — locked; body ignored
- `PUT /api/v1/live/enabled` — `{ liveEnabled, confirmed }`; confirm required to leave `LIVE_DISABLED`
- `PUT /api/v1/live/disabled` · `PUT /api/v1/live/arm` — aliases
- `POST /api/v1/live/pre-order` · `POST /api/v1/live/orders` — **403** `LIVE_DISABLED` unless checklist; armed still returns `LIVE_STUB` (no Fill)

Paper: `POST /api/v1/risk/pre-order` → `POST /api/v1/paper/orders` unchanged.
