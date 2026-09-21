"""Pump.fun bonding-curve math (paper / local simulator).

Uniswap V2-style virtual reserves. Formulas follow pump-public-docs and the
MIT SDK descriptions (spot = virtual_sol / virtual_token; progress from
real_token_reserves). No chain send, no keys.
"""
from __future__ import annotations

from dataclasses import dataclass

# Typical mainnet Global initial values (raw units).
INITIAL_VIRTUAL_SOL_RESERVES = 30_000_000_000  # 30 SOL in lamports
INITIAL_VIRTUAL_TOKEN_RESERVES = 1_073_000_000_000_000
INITIAL_REAL_TOKEN_RESERVES = 793_100_000_000_000
TOKEN_TOTAL_SUPPLY = 1_000_000_000_000_000  # 1B tokens, 6 decimals
TOKEN_DECIMALS = 6
LAMPORTS_PER_SOL = 1_000_000_000
DEFAULT_PROTOCOL_FEE_BPS = 100
DEFAULT_CREATOR_FEE_BPS = 0
# Unsold supply that typically seeds PumpSwap on migrate (~206.9M whole tokens).
AMM_INITIAL_TOKEN_RESERVES = TOKEN_TOTAL_SUPPLY - INITIAL_REAL_TOKEN_RESERVES


def price_sol(virtual_sol_reserves: int, virtual_token_reserves: int) -> float:
    """Spot SOL/token from virtual reserves (AUU: vs / vt). 0 if empty."""
    if virtual_token_reserves <= 0 or virtual_sol_reserves < 0:
        return 0.0
    return virtual_sol_reserves / virtual_token_reserves


def price_sol_str(virtual_sol_reserves: int, virtual_token_reserves: int) -> str:
    px = price_sol(virtual_sol_reserves, virtual_token_reserves)
    return f"{px:.18g}"


def market_cap_lamports(
    virtual_sol_reserves: int, virtual_token_reserves: int, token_total_supply: int
) -> int:
    if virtual_token_reserves <= 0 or token_total_supply <= 0:
        return 0
    return virtual_sol_reserves * token_total_supply // virtual_token_reserves


def market_cap_sol(
    virtual_sol_reserves: int,
    virtual_token_reserves: int,
    token_total_supply: int = TOKEN_TOTAL_SUPPLY,
) -> float:
    return market_cap_lamports(virtual_sol_reserves, virtual_token_reserves, token_total_supply) / (
        LAMPORTS_PER_SOL
    )


def progress_bps(
    real_token_reserves: int,
    initial_real_token_reserves: int = INITIAL_REAL_TOKEN_RESERVES,
) -> int:
    """Graduation progress in bps from real token remaining. 10000 when depleted."""
    if initial_real_token_reserves <= 0:
        return 0
    if real_token_reserves <= 0:
        return 10_000
    remaining = min(int(real_token_reserves), int(initial_real_token_reserves))
    sold = int(initial_real_token_reserves) - remaining
    return int(sold * 10_000 // int(initial_real_token_reserves))


def _total_fee_bps(protocol_fee_bps: int, creator_fee_bps: int) -> int:
    return max(0, int(protocol_fee_bps)) + max(0, int(creator_fee_bps))


def sol_after_buy_fee(
    sol_lamports: int,
    protocol_fee_bps: int = DEFAULT_PROTOCOL_FEE_BPS,
    creator_fee_bps: int = DEFAULT_CREATOR_FEE_BPS,
) -> int:
    """Curve input after protocol+creator fee: (sol - 1) * 10000 / (feeBps + 10000)."""
    if sol_lamports <= 1:
        return 0
    fee = _total_fee_bps(protocol_fee_bps, creator_fee_bps)
    return (int(sol_lamports) - 1) * 10_000 // (fee + 10_000)


def buy_tokens_out(
    virtual_sol_reserves: int,
    virtual_token_reserves: int,
    real_token_reserves: int,
    sol_lamports: int,
    protocol_fee_bps: int = DEFAULT_PROTOCOL_FEE_BPS,
    creator_fee_bps: int = DEFAULT_CREATOR_FEE_BPS,
) -> int:
    """Tokens received for `sol_lamports` spent (capped at real_token_reserves)."""
    inp = sol_after_buy_fee(sol_lamports, protocol_fee_bps, creator_fee_bps)
    if inp <= 0 or virtual_token_reserves <= 0 or virtual_sol_reserves < 0:
        return 0
    tokens = inp * virtual_token_reserves // (virtual_sol_reserves + inp)
    return min(tokens, max(int(real_token_reserves), 0))


def buy_sol_cost_for_tokens(
    virtual_sol_reserves: int,
    virtual_token_reserves: int,
    real_token_reserves: int,
    token_amount: int,
) -> int:
    """Gross lamports (pre-fee) to buy `token_amount` tokens, +1 lamport."""
    amt = min(int(token_amount), max(int(real_token_reserves), 0))
    if amt <= 0 or virtual_token_reserves <= amt:
        return 0
    return amt * virtual_sol_reserves // (virtual_token_reserves - amt) + 1


def sell_sol_out(
    virtual_sol_reserves: int,
    virtual_token_reserves: int,
    token_amount: int,
    protocol_fee_bps: int = DEFAULT_PROTOCOL_FEE_BPS,
    creator_fee_bps: int = DEFAULT_CREATOR_FEE_BPS,
) -> tuple[int, int]:
    """Return (net_sol_to_seller, gross_sol_from_curve)."""
    amt = int(token_amount)
    if amt <= 0 or virtual_sol_reserves <= 0:
        return 0, 0
    gross = amt * virtual_sol_reserves // (virtual_token_reserves + amt)
    fee = _total_fee_bps(protocol_fee_bps, creator_fee_bps)
    fee_amt = gross * fee // 10_000
    net = max(0, gross - fee_amt)
    return net, gross


@dataclass(frozen=True)
class CurveQuote:
    virtual_sol_reserves: int
    virtual_token_reserves: int
    real_sol_reserves: int
    real_token_reserves: int
    tokens_delta: int  # signed: +buy (tokens out) / -sell (tokens in)
    sol_delta: int  # signed: +sol into curve on buy / -sol out on sell
    complete: bool


def apply_buy(
    virtual_sol_reserves: int,
    virtual_token_reserves: int,
    real_sol_reserves: int,
    real_token_reserves: int,
    sol_lamports: int,
    protocol_fee_bps: int = DEFAULT_PROTOCOL_FEE_BPS,
    creator_fee_bps: int = DEFAULT_CREATOR_FEE_BPS,
) -> CurveQuote:
    tokens = buy_tokens_out(
        virtual_sol_reserves,
        virtual_token_reserves,
        real_token_reserves,
        sol_lamports,
        protocol_fee_bps,
        creator_fee_bps,
    )
    if tokens <= 0:
        return CurveQuote(
            virtual_sol_reserves,
            virtual_token_reserves,
            real_sol_reserves,
            real_token_reserves,
            0,
            0,
            real_token_reserves <= 0,
        )
    inp = sol_after_buy_fee(sol_lamports, protocol_fee_bps, creator_fee_bps)
    new_rt = real_token_reserves - tokens
    return CurveQuote(
        virtual_sol_reserves=virtual_sol_reserves + inp,
        virtual_token_reserves=virtual_token_reserves - tokens,
        real_sol_reserves=real_sol_reserves + inp,
        real_token_reserves=new_rt,
        tokens_delta=tokens,
        sol_delta=inp,
        complete=new_rt <= 0,
    )


def apply_sell(
    virtual_sol_reserves: int,
    virtual_token_reserves: int,
    real_sol_reserves: int,
    real_token_reserves: int,
    token_amount: int,
    protocol_fee_bps: int = DEFAULT_PROTOCOL_FEE_BPS,
    creator_fee_bps: int = DEFAULT_CREATOR_FEE_BPS,
    initial_real_token_reserves: int = INITIAL_REAL_TOKEN_RESERVES,
) -> CurveQuote:
    amt = int(token_amount)
    if amt <= 0:
        return CurveQuote(
            virtual_sol_reserves,
            virtual_token_reserves,
            real_sol_reserves,
            real_token_reserves,
            0,
            0,
            real_token_reserves <= 0,
        )
    _net, gross = sell_sol_out(
        virtual_sol_reserves,
        virtual_token_reserves,
        amt,
        protocol_fee_bps,
        creator_fee_bps,
    )
    if gross <= 0 or gross > virtual_sol_reserves or gross > real_sol_reserves:
        return CurveQuote(
            virtual_sol_reserves,
            virtual_token_reserves,
            real_sol_reserves,
            real_token_reserves,
            0,
            0,
            real_token_reserves <= 0,
        )
    new_rt = min(real_token_reserves + amt, initial_real_token_reserves)
    return CurveQuote(
        virtual_sol_reserves=virtual_sol_reserves - gross,
        virtual_token_reserves=virtual_token_reserves + amt,
        real_sol_reserves=real_sol_reserves - gross,
        real_token_reserves=new_rt,
        tokens_delta=-amt,
        sol_delta=-gross,
        complete=new_rt <= 0,
    )


def reserves_at_progress_bps(
    target_bps: int,
    initial_virtual_sol: int = INITIAL_VIRTUAL_SOL_RESERVES,
    initial_virtual_token: int = INITIAL_VIRTUAL_TOKEN_RESERVES,
    initial_real_token: int = INITIAL_REAL_TOKEN_RESERVES,
) -> tuple[int, int, int, int]:
    """Jump the curve to `target_bps` by buying the equivalent token amount (no fee)."""
    bps = max(0, min(10_000, int(target_bps)))
    sold = initial_real_token * bps // 10_000
    if sold <= 0:
        return initial_virtual_sol, initial_virtual_token, 0, initial_real_token
    sold = min(sold, initial_real_token)
    if initial_virtual_token <= sold:
        sold = initial_virtual_token - 1
    sol_cost = buy_sol_cost_for_tokens(
        initial_virtual_sol, initial_virtual_token, initial_real_token, sold
    )
    return (
        initial_virtual_sol + sol_cost,
        initial_virtual_token - sold,
        sol_cost,
        initial_real_token - sold,
    )
