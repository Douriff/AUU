"""Real-market paper: trade decode, frozen price, synthetic filter, flatten, shadow guard."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import struct
import tempfile
import unittest
from pathlib import Path

from app.discovery import DiscoveryRuntime, discovery_accepts
from app.models.contracts import Fill, PumpfunPaperSnapshot, SignalOut
from app.paper.broker import PaperBroker, reset_paper_broker
from app.paper.ledger import build_performance, excluded_from_go, get_paper_journal, reset_paper_ledger
from app.paper.phantom_report import main as phantom_main, report_file
from app.paper.shadow_compare import (
    _Open,
    _opens,
    apply_shadow_config,
    build_shadow_compare,
    observe_candidate,
    reset_shadow_compare,
    shadow_closed,
)
from app.providers.pumpfun_curve_math import (
    INITIAL_REAL_TOKEN_RESERVES,
    INITIAL_VIRTUAL_SOL_RESERVES,
    INITIAL_VIRTUAL_TOKEN_RESERVES,
    price_sol,
)
from app.providers.pumpfun_decode import (
    decode_trade_event,
    extract_trade_from_logs,
    trade_event_discriminator,
)
from app.providers.pumpfun_live_paper import PumpfunLivePaperProvider
from app.providers.pumpfun_paper import PumpfunPaperProvider
from app.risk.gate import reset_risk_gate
from app.strategies.pump_paper_v1 import PumpPaperEngine, PumpPaperParams, TapeWindow, reset_engine


def _pack_trade(
    *,
    sol_amount: int = 50_000_000,
    token_amount: int = 1_000_000_000_000,
    is_buy: bool = True,
    timestamp: int = 1_700_000_000,
    virtual_sol: int = 30_050_000_000,
    virtual_token: int = 1_072_000_000_000_000,
    real_sol: int = 50_000_000,
    real_token: int = 792_100_000_000_000,
    fee_bps: int = 100,
    fee: int = 500_000,
    trailing: bytes = b"",
) -> bytes:
    mint = bytes(range(32))
    user = b"\x11" * 32
    fee_recipient = b"\x22" * 32
    creator = b"\x33" * 32
    body = b"".join(
        [
            trade_event_discriminator(),
            mint,
            struct.pack("<Q", sol_amount),
            struct.pack("<Q", token_amount),
            b"\x01" if is_buy else b"\x00",
            user,
            struct.pack("<q", timestamp),
            struct.pack("<4Q", virtual_sol, virtual_token, real_sol, real_token),
            fee_recipient,
            struct.pack("<Q", fee_bps),
            struct.pack("<Q", fee),
            creator,
            struct.pack("<Q", 0),
            struct.pack("<Q", 0),
            trailing,
        ]
    )
    return body


class TradeDecodeTests(unittest.TestCase):
    def test_trade_event_vector(self):
        raw = _pack_trade(trailing=b"\xff" * 12)
        parsed = decode_trade_event(raw)
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertTrue(parsed["is_buy"])
        self.assertEqual(parsed["sol_amount"], 50_000_000)
        self.assertEqual(parsed["token_amount"], 1_000_000_000_000)
        self.assertEqual(parsed["virtual_sol_reserves"], 30_050_000_000)
        self.assertEqual(parsed["virtual_token_reserves"], 1_072_000_000_000_000)
        self.assertEqual(parsed["real_sol_reserves"], 50_000_000)
        self.assertEqual(parsed["real_token_reserves"], 792_100_000_000_000)
        self.assertEqual(parsed["fee_basis_points"], 100)
        self.assertEqual(parsed["timestamp"], 1_700_000_000)
        self.assertNotEqual(hashlib.sha256(b"event:TradeEvent").digest()[:8], b"\x00" * 8)
        self.assertIsNone(decode_trade_event(b"\x00" * 8 + raw[8:]))
        line = "Program data: " + base64.b64encode(raw).decode()
        again = extract_trade_from_logs(["Program log: Instruction: Buy", line])
        self.assertEqual(again["mint"], parsed["mint"])
        self.assertTrue(again["is_buy"])


class FrozenPriceTests(unittest.TestCase):
    def test_no_print_leaves_price(self):
        provider = PumpfunLivePaperProvider(watch_mints="")
        curve = provider.register_watch_mint(
            "RealMint111111111111111111111111111111111",
            base="REAL",
            reserves={
                "virtual_sol": INITIAL_VIRTUAL_SOL_RESERVES,
                "virtual_token": INITIAL_VIRTUAL_TOKEN_RESERVES,
                "real_sol": 0,
                "real_token": INITIAL_REAL_TOKEN_RESERVES,
            },
            source="test",
        )
        self.assertIsNotNone(curve)
        snap = provider.get_pumpfun_snapshot("REAL/SOL")
        assert snap is not None
        self.assertFalse(snap.synthetic)
        px = snap.price_sol
        self.assertAlmostEqual(px, price_sol(INITIAL_VIRTUAL_SOL_RESERVES, INITIAL_VIRTUAL_TOKEN_RESERVES))
        again = provider.get_pumpfun_snapshot("REAL/SOL")
        assert again is not None
        self.assertEqual(again.price_sol, px)
        self.assertEqual(again.virtual_sol_reserves, snap.virtual_sol_reserves)
        self.assertEqual(provider.get_recent_trades("REAL/SOL"), [])
        self.assertEqual(provider.get_candles("REAL/SOL", "1m"), [])
        self.assertIsNone(provider.register_watch_mint("DemoMintShouldNotEnter", base="NOPE"))
        self.assertIsNone(
            provider.apply_observed_trade(
                {"mint": curve.mint, "side": "buy", "ts": 10, "sol_amount": 0.1}
            )
        )
        still = provider.get_pumpfun_snapshot("REAL/SOL")
        assert still is not None
        self.assertEqual(still.price_sol, px)


class SyntheticFilterTests(unittest.TestCase):
    def setUp(self):
        reset_paper_ledger(wipe_store=True)
        reset_engine()

    def tearDown(self):
        reset_paper_ledger(wipe_store=True)
        reset_engine()

    def test_synthetic_and_legacy_stay_out_of_go(self):
        journal = get_paper_journal()
        journal.record_fill(
            "SYN/SOL",
            Fill(ts=1, price=1.0, qty=1.0, fee=0.0, market_source="synthetic", tag="paper:pump-paper-v1"),
        )
        journal.record_fill(
            "SYN/SOL",
            Fill(ts=2, price=1.2, qty=-1.0, fee=0.0, market_source="synthetic", tag="paper:pump-paper-v1:flat"),
        )
        journal.record_fill(
            "REAL/SOL",
            Fill(ts=3, price=1.0, qty=1.0, fee=0.0, market_source="real", tag="paper:pump-paper-v1"),
        )
        journal.record_fill(
            "REAL/SOL",
            Fill(ts=4, price=0.9, qty=-1.0, fee=0.0, market_source="real", tag="paper:pump-paper-v1:flat"),
        )
        legacy = journal.closed[0].as_dict()
        legacy.pop("market_source", None)
        from app.paper.ledger import RoundTrip

        loaded = RoundTrip.from_dict(legacy)
        self.assertEqual(loaded.market_source, "legacy_synthetic")
        self.assertTrue(excluded_from_go(loaded))
        self.assertNotIn("market_source", loaded.as_dict(persist=True))
        perf = build_performance()
        self.assertEqual(perf["market_window"], "real")
        self.assertEqual(perf["n_trades"], 1)
        self.assertEqual(perf["synthetic_n"], 1)
        self.assertAlmostEqual(perf["win_rate"], 0.0)


class SellFlattenTests(unittest.TestCase):
    def setUp(self):
        reset_paper_ledger(wipe_store=True)
        reset_paper_broker()
        reset_risk_gate()

    def tearDown(self):
        reset_paper_ledger(wipe_store=True)
        reset_paper_broker()

    def test_oversell_does_not_open_short(self):
        journal = get_paper_journal()
        journal.record_fill("PUMPDEMO/SOL", Fill(ts=1, price=1.0, qty=100.0, fee=0.0, market_source="real"))
        journal.record_fill(
            "PUMPDEMO/SOL",
            Fill(ts=2, price=1.1, qty=-100.7, fee=0.0, market_source="real"),
        )
        self.assertEqual(journal.lots.get("PUMPDEMO/SOL"), None)
        self.assertEqual(len(journal.closed), 1)
        self.assertEqual(journal.closed[0].side, "long")
        self.assertAlmostEqual(journal.closed[0].qty, 100.0)

    def test_curve_sell_uses_held_qty(self):
        broker = PaperBroker()
        from app.models.contracts import AccountCtx, LiquidityCtx, OrderIntent, PumpCtx, StrategyContext, TickCtx

        held = 10.0
        ctx = StrategyContext(
            symbol="REAL/SOL",
            ts=1_000,
            account=AccountCtx(),
            liquidity=LiquidityCtx(
                virtual_sol_reserves=str(INITIAL_VIRTUAL_SOL_RESERVES),
                virtual_token_reserves=str(INITIAL_VIRTUAL_TOKEN_RESERVES),
                real_token_reserves=str(INITIAL_REAL_TOKEN_RESERVES),
            ),
            position=held,
            tick=TickCtx(mid=price_sol(INITIAL_VIRTUAL_SOL_RESERVES, INITIAL_VIRTUAL_TOKEN_RESERVES)),
            pump=PumpCtx(
                virtual_sol_reserves=str(INITIAL_VIRTUAL_SOL_RESERVES),
                virtual_token_reserves=str(INITIAL_VIRTUAL_TOKEN_RESERVES),
                real_sol_reserves="0",
                real_token_reserves=str(INITIAL_REAL_TOKEN_RESERVES),
                protocol_fee_bps=100,
            ),
            meta={
                "curve_fill": True,
                "flatten_qty": held,
                "market_source": "real",
                "fill_ts": 5_000,
            },
        )
        fills = broker.submit(
            ctx,
            OrderIntent(
                side="sell",
                order_type="market",
                qty_or_notional=held * ctx.tick.mid * 1.2,
                max_slippage_bps=10_000,
                client_tag="paper:pump-paper-v1:flat",
            ),
        )
        self.assertEqual(len(fills), 1)
        self.assertAlmostEqual(fills[0].qty, -held)
        self.assertEqual(fills[0].market_source, "real")
        self.assertEqual(fills[0].ts, 5_000)
        self.assertGreater(fills[0].fee or 0.0, 0.0)
        journal = get_paper_journal()
        journal.record_fill("REAL/SOL", Fill(ts=1, price=fills[0].price, qty=held, fee=0.0, market_source="real"))
        journal.record_fill("REAL/SOL", fills[0])
        self.assertFalse(journal.lots.get("REAL/SOL"))
        self.assertTrue(all(lot.qty > 0 for lots in journal.lots.values() for lot in lots))


class ShadowProtectTests(unittest.TestCase):
    def setUp(self):
        reset_shadow_compare()
        reset_paper_ledger(wipe_store=True)
        reset_engine()

    def tearDown(self):
        reset_shadow_compare()
        reset_paper_ledger(wipe_store=True)
        reset_engine()

    def test_shadow_open_survives_eviction(self):
        provider = PumpfunPaperProvider(watch_mints="")
        mint = "ShadowMint1111111111111111111111111111111"
        provider.register_watch_mint(mint, base="SHAD", progress_bps=2000, source="pumpportal", max_discovered=1)
        self.assertIn("SHAD/SOL", {c.symbol for c in provider._curves.values()})
        _opens[("loose", "SHAD/SOL")] = _Open(
            symbol="SHAD/SOL",
            mint=mint,
            entry_price=1e-8,
            entry_ts=1,
            entry_notional=0.1,
            tokens=1_000,
            sol_lamports=100_000_000,
        )
        provider.register_watch_mint(
            "OtherMint11111111111111111111111111111111",
            base="OTHR",
            progress_bps=2000,
            source="pumpportal",
            max_discovered=1,
        )
        self.assertIn(mint, provider._by_mint)
        snap = provider.get_pumpfun_snapshot("SHAD/SOL")
        self.assertIsNotNone(snap)
        assert snap is not None
        self.assertGreater(snap.progress_bps, 1000)

    def test_synthetic_snapshot_skips_shadow_column(self):
        apply_shadow_config(
            {"enabled": True, "sets": [{"id": "loose", "min_trade_count_1m": 1, "min_buy_sell_ratio_1m": 0.5}]}
        )
        snap = PumpfunPaperSnapshot(
            mint="DemoMintPump11111111111111111111111111111",
            symbol="PUMPDEMO/SOL",
            phase="curve",
            progress_bps=4200,
            virtual_sol_reserves=str(INITIAL_VIRTUAL_SOL_RESERVES),
            virtual_token_reserves=str(INITIAL_VIRTUAL_TOKEN_RESERVES),
            real_sol_reserves="5000000000",
            real_token_reserves=str(INITIAL_REAL_TOKEN_RESERVES),
            token_total_supply="1000000000000000",
            price_sol=price_sol(INITIAL_VIRTUAL_SOL_RESERVES, INITIAL_VIRTUAL_TOKEN_RESERVES),
            updated_ts=1,
            synthetic=True,
        )
        observe_candidate(
            symbol=snap.symbol,
            snapshot=snap,
            tape=TapeWindow(buy_notional_1m=4.0, sell_notional_1m=1.0, trade_count_1m=12),
            now_ms=1_700_000_000_000,
            notional_sol=0.12,
            impact_entry_bps=20.0,
            main_params=PumpPaperParams(),
        )
        rich = snap.model_copy(
            update={
                "virtual_sol_reserves": str(int(INITIAL_VIRTUAL_SOL_RESERVES * 1.25)),
                "price_sol": price_sol(int(INITIAL_VIRTUAL_SOL_RESERVES * 1.25), INITIAL_VIRTUAL_TOKEN_RESERVES),
            }
        )
        observe_candidate(
            symbol=snap.symbol,
            snapshot=rich,
            tape=TapeWindow(buy_notional_1m=4.0, sell_notional_1m=1.0, trade_count_1m=12),
            now_ms=1_700_000_000_000 + 5_000,
            notional_sol=0.12,
            impact_entry_bps=20.0,
            main_params=PumpPaperParams(),
        )
        self.assertEqual(len(shadow_closed("loose")), 1)
        self.assertEqual(shadow_closed("loose")[0]["market_source"], "synthetic")
        report = build_shadow_compare()
        col = next(s for s in report["sets"] if s["id"] == "loose")
        self.assertEqual(col["n"], 0)


class DiscoveryAcceptTests(unittest.IsolatedAsyncioTestCase):
    def tearDown(self):
        os.environ["DATA_PROVIDER"] = "mock"
        os.environ["PUMPFUN_DISCOVERY"] = "off"
        from app.providers import reset_provider

        reset_provider()

    async def test_rejects_demo_graduated_and_foreign_pool(self):
        sample = {
            "mint": "DiscMint111111111111111111111111111111111",
            "symbol": "NEWCOIN",
            "vSolInBondingCurve": 30.0,
            "vTokensInBondingCurve": 1_073_000_000_000_000,
        }
        self.assertTrue(discovery_accepts(sample, "pumpportal"))
        self.assertFalse(discovery_accepts({**sample, "mint": "DemoMintNope"}, "pumpportal"))
        self.assertFalse(discovery_accepts({**sample, "complete": True}, "pumpportal"))
        self.assertFalse(discovery_accepts({**sample, "pool": "raydium"}, "pumpportal"))
        self.assertTrue(discovery_accepts({**sample, "pool": "pump"}, "pumpportal"))
        os.environ["DATA_PROVIDER"] = "pumpfun_paper"
        os.environ["PUMPFUN_DISCOVERY"] = "off"
        from app.providers import reset_provider

        reset_provider()
        runtime = DiscoveryRuntime()
        ev = await runtime.ingest({**sample, "mint": "DemoMintNope111111111111111111111111"}, "pumpportal")
        self.assertIsNone(ev)
        from app.providers import get_provider

        names = {s.symbol for s in get_provider().list_symbols()}
        self.assertNotIn("NOPE/SOL", names)


class PhantomReportTests(unittest.TestCase):
    def test_dry_run_does_not_write(self):
        payload = {
            "equity_0": 10_000,
            "fills": [],
            "lots": {"A/SOL": [{"symbol": "A/SOL", "qty": -0.2, "price": 1.0, "ts": 1, "fees": 0}]},
            "closed": [{"side": "short", "pnl": 1.0, "symbol": "A/SOL"}],
        }
        fd, name = tempfile.mkstemp(prefix="auu-phantom-", suffix=".json")
        os.close(fd)
        path = Path(name)
        path.write_text(json.dumps(payload), encoding="utf-8")
        before = path.read_bytes()
        try:
            code = phantom_main(["--path", str(path)])
            self.assertEqual(code, 0)
            self.assertEqual(path.read_bytes(), before)
            counts = report_file(path)
            self.assertEqual(counts["open_short_lots"], 1)
            self.assertEqual(counts["closed_short_trades"], 1)
            self.assertEqual(counts["closed_missing_market_source"], 1)
            code_apply = phantom_main(["--path", str(path), "--apply"])
            self.assertEqual(code_apply, 0)
            self.assertEqual(path.read_bytes(), before)
        finally:
            path.unlink(missing_ok=True)


class DeferredFillTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        reset_paper_ledger(wipe_store=True)
        reset_paper_broker()
        reset_risk_gate()
        reset_engine()
        self._prev = os.environ.get("AUTO_PAPER_ORDERS")
        os.environ.pop("AUTO_PAPER_ORDERS", None)

    def tearDown(self):
        reset_paper_ledger(wipe_store=True)
        reset_paper_broker()
        reset_engine()
        if self._prev is None:
            os.environ.pop("AUTO_PAPER_ORDERS", None)
        else:
            os.environ["AUTO_PAPER_ORDERS"] = self._prev

    async def test_fills_on_first_trade_after_delay(self):
        provider = PumpfunLivePaperProvider(watch_mints="")
        mint = "DelayMint11111111111111111111111111111111"
        provider.register_watch_mint(
            mint,
            base="DLAY",
            reserves={
                "virtual_sol": INITIAL_VIRTUAL_SOL_RESERVES,
                "virtual_token": INITIAL_VIRTUAL_TOKEN_RESERVES,
                "real_sol": 0,
                "real_token": INITIAL_REAL_TOKEN_RESERVES,
            },
        )
        snap = provider.get_pumpfun_snapshot("DLAY/SOL")
        assert snap is not None
        engine = PumpPaperEngine(PumpPaperParams(auto_paper_orders=True, max_notional_sol=0.05))
        now = 1_700_000_000_000
        signal = SignalOut(side="long", strength=0.8, reason="pump_paper_v1_entry", tags=["ENTRY"])
        await engine.maybe_execute("DLAY/SOL", snap, signal, now, 0.005)
        self.assertEqual(engine.positions, {})
        self.assertIn("DLAY/SOL", engine._deferred)
        early_vs = INITIAL_VIRTUAL_SOL_RESERVES + 10_000_000
        provider.apply_observed_trade(
            {
                "mint": mint,
                "side": "buy",
                "ts": now + 100,
                "virtual_sol_reserves": early_vs,
                "virtual_token_reserves": INITIAL_VIRTUAL_TOKEN_RESERVES - 1_000_000,
                "real_sol_reserves": 10_000_000,
                "real_token_reserves": INITIAL_REAL_TOKEN_RESERVES,
                "sol_amount": 0.01,
                "token_amount": 1_000_000,
            }
        )
        await engine._drain_real_fill(provider, "DLAY/SOL", now + 200)
        self.assertEqual(engine.positions, {})
        late_vs = INITIAL_VIRTUAL_SOL_RESERVES + 80_000_000
        late_vt = INITIAL_VIRTUAL_TOKEN_RESERVES - 8_000_000
        provider.apply_observed_trade(
            {
                "mint": mint,
                "side": "buy",
                "ts": now + 400,
                "virtual_sol_reserves": late_vs,
                "virtual_token_reserves": late_vt,
                "real_sol_reserves": 80_000_000,
                "real_token_reserves": INITIAL_REAL_TOKEN_RESERVES,
                "sol_amount": 0.08,
                "token_amount": 8_000_000,
            }
        )
        await engine._drain_real_fill(provider, "DLAY/SOL", now + 500)
        self.assertIn("DLAY/SOL", engine.positions)
        pos = engine.positions["DLAY/SOL"]
        self.assertGreater(pos.qty, 0)
        self.assertNotIn("DLAY/SOL", engine._deferred)
        early_px = price_sol(early_vs, INITIAL_VIRTUAL_TOKEN_RESERVES - 1_000_000)
        late_px = price_sol(late_vs, late_vt)
        self.assertLess(abs(pos.entry_price - late_px), abs(pos.entry_price - early_px))


class LiveProviderSourceTests(unittest.TestCase):
    def test_feed_does_not_send(self):
        import inspect

        from app.providers import pumpfun_live_paper as mod

        src = inspect.getsource(mod)
        self.assertIn('"subscribeTokenTrade"', src)
        self.assertNotIn("sendTransaction", src)
        self.assertIn("logsSubscribe", src)
        import app.discovery as disc

        self.assertNotIn('"subscribeTokenTrade"', inspect.getsource(disc))


if __name__ == "__main__":
    unittest.main()
