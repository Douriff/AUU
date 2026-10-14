# AUU signal candidates — pre-registered forward shadow

Registration: **2026-10-15 (Asia/Shanghai)**. Forward window starts with the daily bar `asof_ts = 2026-10-14 00:00 UTC`
(`REG_TS = 1791936000000`), which is the first bar that closes after registration.

Status: **record-only**. Not shown on charts, no orders placed, not read by `trend_tsmom_v1`, live, or paper ledgers.
Code: `apps/api/app/strategies/auu_signal_candidates.py`. Tables `cand_bars`, `cand_trades` and `cand_nav` live in
`auu_signal_shadow.sqlite`. The v0 baseline is written to the same tables under `version = auu_signal_v0`.

| id | definition | params_hash (sha256[:16] of canonical JSON) |
|---|---|---|
| cand_A | v0 factors, long-only, volume-imbalance direction = sign(close[t]−close[t−5]), weights mom .5 / vol .3 / funding .2, enter ≥ 0.25, exit < 0.10, 1d | `6f2f7dea6d308afe` |
| cand_B | same as cand_A without the funding term (mom .5 / vol .3, renormalised), enter ≥ 0.25, exit < 0.10, 1d. **This is the IS-selected candidate (2021–2023 ret/DD).** | `25fad2f2e60c7325` |

| cand_A_ls | cand_A made long/short symmetric: short entry at score ≤ −0.25, short exit at score > −0.10 (mirror of the long rule, exit < 0.10), flip at ±0.25. Registered 2026-10-15. | `3587d21c0a98d883` |
| cand_B_ls | cand_B made long/short symmetric, with the same short rules. Registered 2026-10-15. | `1ee21f59d7664055` |

The parameters are frozen. Any change needs a new id and a new registration date; never edit an existing one in place.
`tests/test_auu_signal_candidates.py` pins both hashes.

## Accounting (hypothetical)
- Fills at the next-bar open.
- Cost: 7 bps per side.
- Funding: settled funding, paid by longs.
- NAV is equal-weight across the strategy universe.
- Trades and NAV only count positions whose signal bar is ≥ REG_TS. A position carried in from before REG_TS counts as flat until the signal changes side. This applies to all candidates and to v0; the NAV side was fixed in PR #108.

## Pre-registration backtest
Backtest setup: 19 coins, Binance spot 1d OHLCV, Binance funding. IS covers 2021–2023, OOS covers 2024-01 to 2026-10.

| | OOS ret / maxDD | OOS per-trade mean / median / win | Chop Feb–Apr 2026 ret / DD | BTC 2026-08 breakout trade | OOS breakout realized capture med |
|---|---|---|---|---|---|
| v0 | −18.4% / −49.1% | −0.01% / −1.01% / 41.5% | −15.4% / −28.4% | +19.88% | 24% |
| cand_A | +45.2% / −35.7% | +1.00% / −1.58% / 36.7% | −8.9% / −9.6% | +20.44% | 40% |
| cand_B | +56.9% / −45.7% | +1.21% / −2.01% / 31.7% | −13.3% / −14.1% | +19.49% | 45% |

Long/short versions, same backtest setup:

| | IS ret / DD | OOS ret / maxDD | OOS per-trade mean / median / win | Chop ret / DD | BTC 2026-08 breakout trade | OOS realized capture med |
|---|---|---|---|---|---|---|
| cand_A_ls | +93.5% / −48.7% | +0.8% / −55.0% | +0.30% / −1.38% / 37.1% | −22.7% / −35.8% | +20.44% (shorts before it: −1.09, −1.01, −1.50) | 32% |
| cand_B_ls | +98.5% / −53.0% | −0.5% / −60.3% | +0.38% / −1.75% / 32.8% | −29.4% / −41.4% | +19.49% (shorts before it: −1.18, −0.90) | 44% |

Neither candidate passed the deploy gate on historical data alone. cand_A was not IS-selected; choosing it after seeing
OOS results would be selection bias. That is why both are being recorded forward.

## Forward evaluation
`/workspace/auu_signal_eval/eval_candidates.py` runs in the weekday 10:38 CST evaluation. It uses only data ≥ REG_TS and
reports post-cost trades, NAV and drawdown for v0, cand_A, cand_B, cand_A_ls and cand_B_ls, plus the `breakout_capture` score.

Breakout events need 20 bars of history and 30 bars of follow-through. Expect no breakout events for roughly the first
6 weeks.

The decision rule is set in advance. A candidate qualifies for an `auu_signal_v1` chart toggle (still labelled 实验) only
if, after at least 90 forward days, all three hold:
1. Its post-cost NAV is better than v0.
2. Its maxDD is no worse than v0.
3. Its breakout realized capture is no worse than v0.
