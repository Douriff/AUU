"""PumpfunPaperProvider + PumpCtx / PaperBroker smoke (no keys)."""
from __future__ import annotations

import os
import unittest

from app.models.contracts import (
    AccountCtx,
    LiquidityCtx,
    OrderIntent,
    PumpCtx,
    SignalOut,
    SizeIn,
    StrategyContext,
    TickCtx,
)
from app.paper.broker import PaperBroker
from app.providers.pumpfun_paper import PumpfunPaperProvider
from app.risk.gate import RiskGate


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.p = PumpfunPaperProvider(watch_mints="")

    def test_builtin_symbols_kind_and_mint(self):
        syms = {s.symbol: s for s in self.p.list_symbols()}
        self.assertIn("PUMPDEMO/SOL", syms)
        self.assertIn("MOONMOCK/SOL", syms)
        self.assertIn("GRADMOCK/SOL", syms)
        for s in syms.values():
            self.assertEqual(s.kind, "pumpfun_curve")
            self.assertEqual(s.quote, "SOL")
            self.assertTrue(s.mint)

    def test_watch_mints_env_shape(self):
        p = PumpfunPaperProvider(watch_mints="MintAAAA:FOO:2500")
        syms = p.list_symbols()
        self.assertEqual(len(syms), 1)
        self.assertEqual(syms[0].symbol, "FOO/SOL")
        snap = p.get_pumpfun_snapshot("FOO/SOL")
        self.assertIsNotNone(snap)
        assert snap is not None
        self.assertEqual(snap.progress_bps, 2500)
        self.assertEqual(snap.mint, "MintAAAA")

    def test_snapshot_fields_and_progress(self):
        snap = self.p.get_pumpfun_snapshot("PUMPDEMO/SOL")
        self.assertIsNotNone(snap)
        assert snap is not None
        self.assertEqual(snap.progress_bps, 4200)
        self.assertFalse(snap.complete)
        self.assertFalse(snap.migrated)
        self.assertGreater(int(snap.virtual_sol_reserves), 0)
        self.assertGreater(int(snap.virtual_token_reserves), 0)
        self.assertGreater(int(snap.real_token_reserves), 0)
        self.assertGreater(snap.price_sol, 0)
        ctx = snap.to_pump_ctx()
        self.assertEqual(ctx.curve_progress_bps, 4200)
        self.assertIsNone(ctx.amm_pool)

        moon = self.p.get_pumpfun_snapshot("MOONMOCK/SOL")
        assert moon is not None
        self.assertGreaterEqual(moon.progress_bps, 9500)
        self.assertLess(moon.progress_bps, 10_000)

        grad = self.p.get_pumpfun_snapshot("GRADMOCK/SOL")
        assert grad is not None
        self.assertEqual(grad.progress_bps, 10_000)
        self.assertTrue(grad.complete)
        self.assertTrue(grad.migrated)
        self.assertEqual(grad.phase, "amm")
        self.assertTrue(grad.pool)

    def test_candles_nonempty(self):
        bars = self.p.get_candles("PUMPDEMO/SOL", "1m")
        self.assertGreater(len(bars), 50)
        self.assertEqual(bars[-1].symbol, "PUMPDEMO/SOL")

    def test_name(self):
        self.assertEqual(self.p.name, "pumpfun_paper")

    def test_stream_emits_curve_and_trades(self):
        import asyncio

        async def run():
            agen = self.p.stream("trades", "GRADMOCK/SOL")
            types = []
            for _ in range(4):
                msg = await asyncio.wait_for(agen.__anext__(), 2.5)
                types.append(msg["type"])
                if msg["type"] == "trade":
                    self.assertIn(msg["payload"]["side"], ("buy", "sell"))
            await agen.aclose()
            return types

        types = asyncio.run(run())
        self.assertIn("pumpfun_curve", types)
        self.assertTrue(any(t == "trade" for t in types))


class RiskAndPaperTests(unittest.TestCase):
    def test_curve_near_graduation_tag(self):
        gate = RiskGate()
        ctx = StrategyContext(
            symbol="MOONMOCK/SOL",
            ts=1,
            account=AccountCtx(equity=10_000, day_pnl=0),
            liquidity=LiquidityCtx(spread_bps=20, adv_usd=100_000),
            tick=TickCtx(mid=0.0001),
            pump=PumpCtx(
                curve_progress_bps=9700,
                virtual_sol_reserves="1",
                virtual_token_reserves="1",
                real_sol_reserves="1",
                real_token_reserves="1",
                creator_fee_bps=0,
                complete=False,
                migrated=False,
            ),
        )
        out = gate.check(ctx, SignalOut(side="long"), SizeIn(target_notional=200, max_slippage_bps=500))
        self.assertTrue(out.allow)
        self.assertIn("CURVE_NEAR_GRADUATION", out.tags)

    def test_paper_broker_still_fills_with_pump_ctx(self):
        broker = PaperBroker()
        ctx = StrategyContext(
            symbol="PUMPDEMO/SOL",
            ts=1_000,
            tick=TickCtx(mid=0.00003),
            liquidity=LiquidityCtx(spread_bps=20, adv_usd=100_000),
            pump=PumpCtx(curve_progress_bps=4200, complete=False, migrated=False),
        )
        fills = broker.submit(
            ctx,
            OrderIntent(side="buy", order_type="market", qty_or_notional=500, client_tag="paper"),
        )
        self.assertEqual(len(fills), 1)
        self.assertGreater(fills[0].price, 0)
        self.assertEqual(fills[0].tag, "paper")


class NoKeysGuardTests(unittest.TestCase):
    def test_provider_module_has_no_keypair(self):
        root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        banned = ("Keypair.from", "WALLET_PRIVATE_KEY", "searcher-keypair", "jito_bundle")
        hits: list[str] = []
        for dirpath, _dirs, files in os.walk(os.path.join(root, "app")):
            for fn in files:
                if not fn.endswith(".py"):
                    continue
                path = os.path.join(dirpath, fn)
                with open(path, encoding="utf-8") as fh:
                    text = fh.read()
                for word in banned:
                    if word in text:
                        hits.append(f"{path}:{word}")
        self.assertEqual(hits, [])


if __name__ == "__main__":
    unittest.main()
