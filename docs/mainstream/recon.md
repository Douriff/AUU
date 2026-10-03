# Cross-source daily close reconciliation (P1-3)

- Module: `apps/api/app/marketdata/mainstream/recon.py`. Loop starts with the API (`AUU_RECON=on` by default).
- After `AUU_RECON_AFTER_MIN` (20) minutes past 00:00 UTC (08:20 Beijing), once per UTC day: for each strategy coin, the
  last `AUU_RECON_DAYS` (7) **closed** daily bars from the local store (the venue the strategy reads) are compared with
  the other venue's public REST klines (Binance `api/v3/klines`, OKX `api/v5/market/history-candles` `bar=1Dutc`).
  One keyless GET per coin, paced 0.25 s, plain urllib (no ccxt client → no extra memory).
- Flags: `deviation` (|close2/close1 − 1| > threshold, default `AUU_RECON_THRESHOLD_PCT=0.5`, per coin
  `AUU_RECON_THRESHOLDS="DOGE:1.0,FIL:1.0"`), `missing_primary`, `missing_secondary`. A venue error is not a finding;
  the run retries every `AUU_RECON_RETRY_MIN` (30) minutes, at most 3 times a day.
- New findings go through the existing alert email (kind `recon`, each coin/day/status mailed once; the alert center's
  dedup and rate limits apply). The strategy data source is **never** switched automatically.
- Results: `recon.sqlite` in the data dir (400 days kept). `GET /api/v1/mainstream/recon` (login) for the console panel;
  `/api/v1/health` → `recon` (coarse, no prices). CLI: `python -m app.marketdata.mainstream.recon run|status`.
