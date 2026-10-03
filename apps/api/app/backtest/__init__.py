"""M2 backtest engine (self-built, stdlib only; research reference: plan §2).

- :mod:`panel`     daily spot/perp/funding panel (Binance public archive or the AUU market store)
- :mod:`costs`     taker fee + per-coin slippage (shared with paper)
- :mod:`engine`    daily portfolio loop: drift, rebalance band, costs, funding
- :mod:`stats`     annualized mean/CAGR/Sharpe, block-bootstrap CI, MDD, worst month, ex-best-3-months
- :mod:`research`  hold-out (last 30%), walk-forward, 2x cost, +1d lag, benchmarks, look-ahead check
- :mod:`acceptance` reproduces the research numbers (plan §2.2) within 0.1 pp

Strategies live in :mod:`app.strategies` and are the same objects the paper runner calls.
"""
