"""Buy vs sell curve impact wiring (paper-only). No wallets."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from app.models.contracts import (
    AccountCtx,
    LiquidityCtx,
    PumpCtx,
    SignalOut,
    SizeIn,
    StrategyContext,
)
from app.providers.pumpfun_curve_math import (
    DEFAULT_IMPACT_FEE_BPS,
    DEFAULT_PROTOCOL_FEE_BPS,
    buy_tokens_out,
    estimated_curve_impact_bps,
    price_sol,
    reserves_at_progress_bps,
    sell_sol_out,
    sol_after_buy_fee,
)
from app.risk.gate import RiskGate


def _pump_at(progress_bps: int, *, complete: bool = False, migrated: bool = False, fee_bps=None) -> PumpCtx:
    vs, vt, rs, rt = reserves_at_progress_bps(progress_bps)
    return PumpCtx(
        curve_progress_bps=progress_bps,
        virtual_sol_reserves=str(vs),
        virtual_token_reserves=str(vt),
        real_sol_reserves=str(rs),
        real_token_reserves=str(rt),
        creator_fee_bps=0,
        fee_bps=fee_bps,
        complete=complete,
        migrated=migrated,
    )


def _liq() -> LiquidityCtx:
    return LiquidityCtx(spread_bps=20, adv_usd=100_000)


class CexFallbackTests(unittest.TestCase):
    def test_without_pump_keeps_sqrt_impact(self):
        liq = _liq()
        notional = 200.0
        expected = 20.0 / 2 + 40.0 * ((notional / 100_000.0) ** 0.6)
        self.assertAlmostEqual(liq.estimated_impact_bps(notional), expected)
        # empty pump object has zero reserves → still CEX
        empty = PumpCtx(curve_progress_bps=100, complete=False, migrated=False)
        self.assertAlmostEqual(
            liq.estimated_impact_bps(notional, side="buy", pump=empty),
            expected,
        )


class CurveFormulaTests(unittest.TestCase):
    def test_buy_matches_fee_aware_tokens_out_and_mid_move(self):
        vs, vt, rs, rt = reserves_at_progress_bps(4200)
        notional = 0.25
        fee = DEFAULT_IMPACT_FEE_BPS
        proto = DEFAULT_PROTOCOL_FEE_BPS
        got = estimated_curve_impact_bps(vs, vt, rs, rt, notional, "buy", fee_bps=fee)
        sol_lamports = int(notional * 1_000_000_000)
        tokens = buy_tokens_out(vs, vt, rt, sol_lamports, proto, 0)
        net = sol_after_buy_fee(sol_lamports, proto, 0)
        mid0 = price_sol(vs, vt)
        avg = net / tokens
        mid1 = price_sol(vs + net, vt - tokens)
        want = max(abs(avg - mid0) / mid0, abs(mid1 - mid0) / mid0) * 10_000 + fee / 2
        self.assertAlmostEqual(got, want, places=9)

    def test_sell_matches_sell_sol_out_and_mid_move(self):
        vs, vt, rs, rt = reserves_at_progress_bps(4200)
        notional = 0.25
        fee = DEFAULT_IMPACT_FEE_BPS
        proto = DEFAULT_PROTOCOL_FEE_BPS
        got = estimated_curve_impact_bps(vs, vt, rs, rt, notional, "sell", fee_bps=fee)
        sol_lamports = int(notional * 1_000_000_000)
        token_amount = sol_lamports * vt // vs
        _net, gross = sell_sol_out(vs, vt, token_amount, proto, 0)
        mid0 = price_sol(vs, vt)
        avg = gross / token_amount
        mid1 = price_sol(vs - gross, vt + token_amount)
        want = max(abs(avg - mid0) / mid0, abs(mid1 - mid0) / mid0) * 10_000 + fee / 2
        self.assertAlmostEqual(got, want, places=9)

    def test_buy_fee_aware_sol_path_uses_protocol_fee(self):
        vs, vt, rs, rt = reserves_at_progress_bps(4200)
        no_proto = estimated_curve_impact_bps(
            vs, vt, rs, rt, 0.5, "buy", fee_bps=0, protocol_fee_bps=0
        )
        with_proto = estimated_curve_impact_bps(
            vs, vt, rs, rt, 0.5, "buy", fee_bps=0, protocol_fee_bps=100
        )
        self.assertNotAlmostEqual(no_proto, with_proto, places=4)

    def test_smaller_notional_yields_lower_buy_impact(self):
        """Curve impact rises with size. Paper clips rely on this, not on a fake fill."""
        vs, vt, rs, rt = reserves_at_progress_bps(4200)
        larger = estimated_curve_impact_bps(vs, vt, rs, rt, 0.05, "buy")
        smaller = estimated_curve_impact_bps(vs, vt, rs, rt, 0.01, "buy")
        self.assertGreater(larger, 0)
        self.assertGreater(smaller, 0)
        self.assertLess(smaller, larger)
        # Same ordering under the paper quote fee (100) and the library default (125).
        paper_large = estimated_curve_impact_bps(vs, vt, rs, rt, 0.05, "buy", fee_bps=100)
        paper_small = estimated_curve_impact_bps(vs, vt, rs, rt, 0.01, "buy", fee_bps=100)
        self.assertLess(paper_small, paper_large)
        self.assertLess(paper_small, smaller)

    def test_buy_and_sell_impacts_differ_same_notional(self):
        vs, vt, rs, rt = reserves_at_progress_bps(4200)
        buy = estimated_curve_impact_bps(vs, vt, rs, rt, 0.4, "buy")
        sell = estimated_curve_impact_bps(vs, vt, rs, rt, 0.4, "sell")
        self.assertGreater(buy, 0)
        self.assertGreater(sell, 0)
        self.assertNotAlmostEqual(buy, sell, places=4)

    def test_complete_migrated_do_not_enter_cp_formula(self):
        liq = _liq()
        same_res_open = _pump_at(4200, complete=False, migrated=False)
        same_res_done = _pump_at(4200, complete=True, migrated=True)
        buy_a = liq.estimated_impact_bps(0.3, side="buy", pump=same_res_open)
        buy_b = liq.estimated_impact_bps(0.3, side="buy", pump=same_res_done)
        sell_a = liq.estimated_impact_bps(0.3, side="sell", pump=same_res_open)
        sell_b = liq.estimated_impact_bps(0.3, side="sell", pump=same_res_done)
        self.assertAlmostEqual(buy_a, buy_b)
        self.assertAlmostEqual(sell_a, sell_b)
        self.assertNotAlmostEqual(buy_a, sell_a, places=4)

    def test_default_fee_125_overridable(self):
        vs, vt, rs, rt = reserves_at_progress_bps(4200)
        # Add-on is independent of protocol/creator used on the fee-aware SOL path.
        base = estimated_curve_impact_bps(vs, vt, rs, rt, 0.2, "buy", fee_bps=0)
        defaulted = estimated_curve_impact_bps(vs, vt, rs, rt, 0.2, "buy")
        override = estimated_curve_impact_bps(vs, vt, rs, rt, 0.2, "buy", fee_bps=200)
        self.assertEqual(DEFAULT_IMPACT_FEE_BPS, 125)
        self.assertAlmostEqual(defaulted - base, 62.5, places=6)
        self.assertAlmostEqual(override - base, 100.0, places=6)
        liq = _liq()
        via_ctx = liq.estimated_impact_bps(0.2, side="buy", pump=_pump_at(4200, fee_bps=200))
        self.assertAlmostEqual(via_ctx, override, places=6)

    def test_liquidity_pump_fields_without_pump_ctx(self):
        vs, vt, rs, rt = reserves_at_progress_bps(4200)
        liq = LiquidityCtx(
            spread_bps=20,
            adv_usd=100_000,
            virtual_sol_reserves=str(vs),
            virtual_token_reserves=str(vt),
            real_sol_reserves=str(rs),
            real_token_reserves=str(rt),
        )
        via_fields = liq.estimated_impact_bps(0.2, side="sell")
        via_fn = estimated_curve_impact_bps(vs, vt, rs, rt, 0.2, "sell")
        self.assertAlmostEqual(via_fields, via_fn, places=9)
        cex = LiquidityCtx(spread_bps=20, adv_usd=100_000).estimated_impact_bps(0.2)
        self.assertNotAlmostEqual(via_fields, cex, places=2)

    def test_missing_side_does_not_silently_assume_buy(self):
        liq = _liq()
        with self.assertRaises(ValueError):
            liq.estimated_impact_bps(0.2, pump=_pump_at(4200))


class BuySellBranchIsolationTests(unittest.TestCase):
    """Buy must not share the sell branch (and vice versa)."""

    def test_buy_calls_buy_tokens_out_not_sell_sol_out(self):
        vs, vt, rs, rt = reserves_at_progress_bps(4200)
        with (
            patch("app.providers.pumpfun_curve_math.buy_tokens_out", wraps=buy_tokens_out) as buy_fn,
            patch("app.providers.pumpfun_curve_math.sell_sol_out", wraps=sell_sol_out) as sell_fn,
        ):
            estimated_curve_impact_bps(vs, vt, rs, rt, 0.15, "buy")
            self.assertTrue(buy_fn.called)
            self.assertFalse(sell_fn.called)

    def test_sell_calls_sell_sol_out_not_buy_tokens_out(self):
        vs, vt, rs, rt = reserves_at_progress_bps(4200)
        with (
            patch("app.providers.pumpfun_curve_math.buy_tokens_out", wraps=buy_tokens_out) as buy_fn,
            patch("app.providers.pumpfun_curve_math.sell_sol_out", wraps=sell_sol_out) as sell_fn,
        ):
            estimated_curve_impact_bps(vs, vt, rs, rt, 0.15, "sell")
            self.assertTrue(sell_fn.called)
            self.assertFalse(buy_fn.called)

    def test_long_alias_is_buy_branch_short_is_sell(self):
        vs, vt, rs, rt = reserves_at_progress_bps(4200)
        self.assertAlmostEqual(
            estimated_curve_impact_bps(vs, vt, rs, rt, 0.15, "long"),
            estimated_curve_impact_bps(vs, vt, rs, rt, 0.15, "buy"),
        )
        self.assertAlmostEqual(
            estimated_curve_impact_bps(vs, vt, rs, rt, 0.15, "short"),
            estimated_curve_impact_bps(vs, vt, rs, rt, 0.15, "sell"),
        )


class RiskGateWiringTests(unittest.TestCase):
    def _ctx(self, pump: PumpCtx) -> StrategyContext:
        return StrategyContext(
            symbol="PUMPDEMO/SOL",
            ts=1,
            account=AccountCtx(equity=10_000, day_pnl=0),
            liquidity=_liq(),
            pump=pump,
        )

    def test_long_and_short_use_separate_impacts(self):
        gate = RiskGate()
        ctx = self._ctx(_pump_at(4200))
        # Size large enough that curve impact exceeds the 150 bps cap on at least one side.
        size = SizeIn(target_notional=8.0, max_slippage_bps=10_000)
        long_out = gate.check(ctx, SignalOut(side="long"), size)
        short_out = gate.check(ctx, SignalOut(side="short"), size)
        buy_bps = ctx.liquidity.estimated_impact_bps(8.0, side="buy", pump=ctx.pump)
        sell_bps = ctx.liquidity.estimated_impact_bps(8.0, side="sell", pump=ctx.pump)
        self.assertNotAlmostEqual(buy_bps, sell_bps, places=4)
        # Gate must not apply the other side's impact: allow flags follow each side.
        self.assertEqual(long_out.allow, buy_bps <= 150.0)
        self.assertEqual(short_out.allow, sell_bps <= 150.0)
        if long_out.allow != short_out.allow:
            denied = long_out if not long_out.allow else short_out
            self.assertIn("SLIPPAGE_CAP", denied.tags)

    def test_near_graduation_multiplies_after_curve_impact(self):
        gate = RiskGate()
        pump = _pump_at(9700, complete=False, migrated=False)
        ctx = self._ctx(pump)
        base = ctx.liquidity.estimated_impact_bps(0.05, side="buy", pump=pump)
        self.assertLess(base * 1.5, 150.0)
        out = gate.check(
            ctx,
            SignalOut(side="long"),
            SizeIn(target_notional=0.05, max_slippage_bps=500),
        )
        self.assertTrue(out.allow)
        self.assertIn("CURVE_NEAR_GRADUATION", out.tags)

    def test_complete_flag_is_gate_not_formula(self):
        """Same reserves: complete only adds the ×1.5 tag path, not a different CP."""
        gate = RiskGate()
        open_p = _pump_at(4200, complete=False)
        done_p = _pump_at(4200, complete=True)
        liq = _liq()
        self.assertAlmostEqual(
            liq.estimated_impact_bps(0.05, side="buy", pump=open_p),
            liq.estimated_impact_bps(0.05, side="buy", pump=done_p),
        )
        tagged = gate.check(
            self._ctx(done_p),
            SignalOut(side="long"),
            SizeIn(target_notional=0.05, max_slippage_bps=500),
        )
        untagged = gate.check(
            self._ctx(open_p),
            SignalOut(side="long"),
            SizeIn(target_notional=0.05, max_slippage_bps=500),
        )
        self.assertIn("CURVE_NEAR_GRADUATION", tagged.tags)
        self.assertNotIn("CURVE_NEAR_GRADUATION", untagged.tags)


if __name__ == "__main__":
    unittest.main()
