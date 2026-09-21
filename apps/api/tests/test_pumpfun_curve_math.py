"""Unit tests for Pump.fun paper curve math + decoder (no RPC, no keys)."""
from __future__ import annotations

import unittest

from app.providers.pumpfun_curve_math import (
    INITIAL_REAL_TOKEN_RESERVES,
    INITIAL_VIRTUAL_SOL_RESERVES,
    INITIAL_VIRTUAL_TOKEN_RESERVES,
    TOKEN_TOTAL_SUPPLY,
    apply_buy,
    buy_tokens_out,
    price_sol,
    progress_bps,
    reserves_at_progress_bps,
)
from app.providers.pumpfun_decode import decode_bonding_curve, encode_bonding_curve_body


class ProgressBpsTests(unittest.TestCase):
    def test_zero_when_unsold(self):
        self.assertEqual(progress_bps(INITIAL_REAL_TOKEN_RESERVES), 0)

    def test_complete_when_depleted(self):
        self.assertEqual(progress_bps(0), 10_000)

    def test_half(self):
        half = INITIAL_REAL_TOKEN_RESERVES // 2
        self.assertEqual(progress_bps(half), 5000)


class PriceTests(unittest.TestCase):
    def test_initial_spot_matches_vs_over_vt(self):
        px = price_sol(INITIAL_VIRTUAL_SOL_RESERVES, INITIAL_VIRTUAL_TOKEN_RESERVES)
        expected = INITIAL_VIRTUAL_SOL_RESERVES / INITIAL_VIRTUAL_TOKEN_RESERVES
        self.assertAlmostEqual(px, expected, places=18)
        self.assertAlmostEqual(px, 2.795e-5, delta=1e-7)


class BuyImpactTests(unittest.TestCase):
    def test_buy_0_1_sol_ignoring_fee_ballpark(self):
        # Worked example (no fee): ~3.56e12 raw tokens for 0.1 SOL
        tokens = buy_tokens_out(
            INITIAL_VIRTUAL_SOL_RESERVES,
            INITIAL_VIRTUAL_TOKEN_RESERVES,
            INITIAL_REAL_TOKEN_RESERVES,
            100_000_000,
            protocol_fee_bps=0,
            creator_fee_bps=0,
        )
        self.assertGreater(tokens, 3.4e12)
        self.assertLess(tokens, 3.7e12)

    def test_apply_buy_moves_reserves_and_progress(self):
        q = apply_buy(
            INITIAL_VIRTUAL_SOL_RESERVES,
            INITIAL_VIRTUAL_TOKEN_RESERVES,
            0,
            INITIAL_REAL_TOKEN_RESERVES,
            1_000_000_000,
        )
        self.assertGreater(q.tokens_delta, 0)
        self.assertGreater(q.virtual_sol_reserves, INITIAL_VIRTUAL_SOL_RESERVES)
        self.assertLess(q.real_token_reserves, INITIAL_REAL_TOKEN_RESERVES)
        self.assertGreater(progress_bps(q.real_token_reserves), 0)
        self.assertFalse(q.complete)

    def test_seed_progress_exact_bps(self):
        vs, vt, rs, rt = reserves_at_progress_bps(4200)
        self.assertEqual(progress_bps(rt), 4200)
        self.assertGreater(vs, INITIAL_VIRTUAL_SOL_RESERVES)
        self.assertEqual(vs - INITIAL_VIRTUAL_SOL_RESERVES, rs)

    def test_graduation_about_85_sol(self):
        vs, vt, rs, rt = reserves_at_progress_bps(10_000)
        self.assertEqual(rt, 0)
        self.assertEqual(progress_bps(rt), 10_000)
        # ~85 SOL accumulated
        self.assertGreater(rs, 80_000_000_000)
        self.assertLess(rs, 90_000_000_000)


class DecodeTests(unittest.TestCase):
    def test_roundtrip(self):
        blob = encode_bonding_curve_body(
            INITIAL_VIRTUAL_TOKEN_RESERVES,
            INITIAL_VIRTUAL_SOL_RESERVES,
            INITIAL_REAL_TOKEN_RESERVES,
            0,
            TOKEN_TOTAL_SUPPLY,
            False,
        )
        decoded = decode_bonding_curve(blob)
        self.assertEqual(decoded["virtual_sol_reserves"], INITIAL_VIRTUAL_SOL_RESERVES)
        self.assertEqual(decoded["real_token_reserves"], INITIAL_REAL_TOKEN_RESERVES)
        self.assertFalse(decoded["complete"])


if __name__ == "__main__":
    unittest.main()
