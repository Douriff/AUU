"""Platform points (积分): a per-account balance spent inside the platform.

Completely separate from paper trading: its own sqlite file (points.sqlite), never read or
written by any paper / strategy / H2 code, and paper P&L never touches it.

Optional modules (all default OFF):
  * USDT-TRC20 top-up (``trc20``)
  * Points trading — capital = available points, fills on real market data, P&L credits/debits
    points (``trading``; flag ``AUU_POINTS_TRADING``)
  * Withdrawal — user applies → freeze → admin manual USDT-TRC20 payout from TokenPocket
    (private key never on server) → on-chain verify → complete (``withdraw``; flag
    ``AUU_WITHDRAW_ENABLED``)
"""
