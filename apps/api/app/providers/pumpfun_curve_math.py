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
# Paper impact add-on / fee-aware path default (overridable).
DEFAULT_IMPACT_FEE_BPS = 125
# Flat fee inside estimated_curve_impact_bps: the formula adds fee_bps/2.
# Default 125/2 = 62.5 bps. This is the bonding-curve protocol-fee floor
# subtracted for Go net-of-fee. It is NOT DEFAULT_PROTOCOL_FEE_BPS (100),
# which is already inside the fee-aware reserve walk (sol_after_buy_fee).
CURVE_IMPACT_FEE_FLOOR_BPS = DEFAULT_IMPACT_FEE_BPS / 2.0  # 62.5
# AMM / migrated (GRADMOCK): virtual reserves are cleared, so
# LiquidityCtx.estimated_impact_bps falls back to CEX
# `spread_bps/2 + 40*(notional/adv)^0.6`. Default spread is 20 → floor 10 bps.
# Kept here so Go uses the model floor, not a handwritten magic number.
DEFAULT_LIQUIDITY_SPREAD_BPS = 20.0
AMM_IMPACT_FEE_FLOOR_BPS = DEFAULT_LIQUIDITY_SPREAD_BPS / 2.0  # 10.0
# When the curve cannot fill, report a full-notional shock rather than 0.
UNFILLABLE_IMPACT_BPS = 10_000.0
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


def impact_venue_phase(pump: object | None = None, phase: str | None = None) -> str:
    """curve | graduating | amm. Unknown / missing → curve (paper entry venue)."""
    if phase:
        p = str(phase).strip().lower()
        if p in {"curve", "graduating", "amm"}:
            return p
    if pump is not None:
        if bool(getattr(pump, "migrated", False)):
            return "amm"
        explicit = getattr(pump, "phase", None)
        if explicit in {"curve", "graduating", "amm"}:
            return str(explicit)
        if bool(getattr(pump, "complete", False)):
            return "graduating"
    return "curve"


def protocol_fee_bps_for_phase(
    phase: str | None = "curve",
    *,
    impact_fee_bps: float | None = None,
) -> float:
    """Phase-aware flat fee embedded in estimated impact (Go net-of-fee).

    curve / graduating: ``impact_fee_bps/2``, default ``CURVE_IMPACT_FEE_FLOOR_BPS``
    (62.5). amm: ``AMM_IMPACT_FEE_FLOOR_BPS`` (10), the CEX half-spread floor used
    once a mint has migrated and curve reserves are gone.
    """
    venue = impact_venue_phase(phase=phase)
    if venue == "amm":
        return float(AMM_IMPACT_FEE_FLOOR_BPS)
    if impact_fee_bps is None:
        return float(CURVE_IMPACT_FEE_FLOOR_BPS)
    return max(0.0, float(impact_fee_bps) / 2.0)


def impact_net_bps(impact_gross_bps: float, protocol_fee_bps: float) -> float:
    """``max(0, impact_gross_bps - protocol_fee_bps)``."""
    return max(0.0, float(impact_gross_bps) - float(protocol_fee_bps))


def _bps_vs_mid(px: float, mid0: float) -> float:
    if mid0 <= 0 or px <= 0:
        return UNFILLABLE_IMPACT_BPS
    return abs(px - mid0) / mid0 * 10_000.0


def _normalize_impact_side(side: str | None) -> str:
    """Map long/short aliases; do not default buy↔sell to the same branch."""
    s = (side or "").strip().lower()
    if s in {"buy", "long"}:
        return "buy"
    if s in {"sell", "short"}:
        return "sell"
    raise ValueError(f"curve impact requires side buy|sell (or long|short), got {side!r}")


def estimated_curve_impact_bps(
    virtual_sol_reserves: int,
    virtual_token_reserves: int,
    real_sol_reserves: int,
    real_token_reserves: int,
    notional_sol: float,
    side: str,
    fee_bps: int = DEFAULT_IMPACT_FEE_BPS,
    protocol_fee_bps: int | None = None,
    creator_fee_bps: int = 0,
) -> float:
    """Paper curve impact in bps: max(avg-price vs mid0, mid move) + fee_bps/2.

    Buy uses ``buy_tokens_out`` + ``sol_after_buy_fee`` (protocol+creator on the
    SOL in path; default protocol 100 bps). Sell uses ``sell_sol_out``.
    The two paths are not a single signed constant-product branch.

    ``fee_bps`` is the impact add-on (default 125, overridable). ``complete`` /
    ``migrated`` are *not* inputs: callers may apply a near-graduation
    multiplier separately. Only virtual/real reserves feed the formula.
    """
    vs = int(virtual_sol_reserves)
    vt = int(virtual_token_reserves)
    rs = int(real_sol_reserves)
    rt = int(real_token_reserves)
    addon = max(0, int(fee_bps))
    proto = int(protocol_fee_bps) if protocol_fee_bps is not None else DEFAULT_PROTOCOL_FEE_BPS
    creator = max(0, int(creator_fee_bps))
    direction = _normalize_impact_side(side)

    mid0 = price_sol(vs, vt)
    if mid0 <= 0 or vs <= 0 or vt <= 0 or rs < 0 or rt < 0:
        return UNFILLABLE_IMPACT_BPS + addon / 2.0

    sol_lamports = int(abs(float(notional_sol)) * LAMPORTS_PER_SOL)
    if sol_lamports <= 0:
        return addon / 2.0

    if direction == "buy":
        tokens = buy_tokens_out(vs, vt, rt, sol_lamports, proto, creator)
        net_sol = sol_after_buy_fee(sol_lamports, proto, creator)
        if tokens <= 0 or net_sol <= 0:
            return UNFILLABLE_IMPACT_BPS + addon / 2.0
        avg_px = net_sol / tokens
        new_vs = vs + net_sol
        new_vt = vt - tokens
        mid1 = price_sol(new_vs, new_vt)
    else:
        # Quote notional → tokens at mid0; SOL out via sell_sol_out (not buy).
        token_amount = sol_lamports * vt // vs
        if token_amount <= 0:
            return UNFILLABLE_IMPACT_BPS + addon / 2.0
        _net, gross = sell_sol_out(vs, vt, token_amount, proto, creator)
        if gross <= 0 or gross > vs:
            return UNFILLABLE_IMPACT_BPS + addon / 2.0
        avg_px = gross / token_amount
        new_vs = vs - gross
        new_vt = vt + token_amount
        mid1 = price_sol(new_vs, new_vt)

    avg_bps = _bps_vs_mid(avg_px, mid0)
    mid_bps = _bps_vs_mid(mid1, mid0) if mid1 > 0 else UNFILLABLE_IMPACT_BPS
    return max(avg_bps, mid_bps) + addon / 2.0


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
