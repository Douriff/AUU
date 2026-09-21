"""pump-paper-v1 evaluate + tape + auto_paper_orders (paper only)."""
from __future__ import annotations

import os
import time
import unittest

from app.models.contracts import PumpfunPaperSnapshot
from app.paper.broker import reset_paper_broker
from app.providers import reset_provider
from app.risk.gate import reset_risk_gate
from app.strategies.pump_paper_v1 import (
    PositionState,
    PumpPaperEngine,
    PumpPaperParams,
    TapeWindow,
    aggregate_tape,
    evaluate,
    reset_engine,
)


def _snap(**kwargs) -> PumpfunPaperSnapshot:
    base = dict(
        mint="DemoMintPump11111111111111111111111111111",
        symbol="PUMPDEMO/SOL",
        phase="curve",
        progress_bps=4200,
        complete=False,
        migrated=False,
        virtual_sol_reserves="30000000000",
        virtual_token_reserves="1073000000000000",
        real_sol_reserves="5000000000",
        real_token_reserves="793100000000000",
        token_total_supply="1000000000000000",
        price_sol=2.8e-5,
        creator_fee_bps=0,
        updated_ts=1,
        synthetic=True,
    )
    base.update(kwargs)
    return PumpfunPaperSnapshot(**base)


def _hot_tape() -> TapeWindow:
    return TapeWindow(buy_notional_1m=4.0, sell_notional_1m=1.0, trade_count_1m=12)


class ParamsDefaultsTests(unittest.TestCase):
    def test_frozen_v1(self):
        p = PumpPaperParams()
        # Paper round 4 Go window. Hard reject remains gross impact > 80.
        self.assertEqual(p.progress_bps_min, 1200)
        self.assertEqual(p.progress_bps_max, 6500)
        self.assertEqual(p.max_impact_bps, 75.0)
        self.assertLess(p.max_impact_bps, 80.0)
        self.assertAlmostEqual(p.notional_pct_equity, 0.005)
        self.assertFalse(p.auto_paper_orders)
        self.assertAlmostEqual(p.take_profit_pct, 0.10)
        self.assertAlmostEqual(p.stop_loss_pct, 0.07)
        self.assertGreater(p.take_profit_pct, p.stop_loss_pct)
        self.assertEqual(p.max_hold_sec, 300)
        self.assertLess(p.max_hold_sec, 900)
        self.assertEqual(p.max_day_loss_pct, 0.05)
        self.assertEqual(p.max_open_mints, 3)
        self.assertEqual(p.max_notional_sol, 0.12)


class TapeTests(unittest.TestCase):
    def test_aggregate_1m(self):
        now = 1_000_000
        trades = [
            {"ts": now - 1_000, "side": "buy", "sol_amount": 1.5},
            {"ts": now - 2_000, "side": "sell", "sol_amount": 0.4},
            {"ts": now - 120_000, "side": "buy", "sol_amount": 9.0},
        ]
        w = aggregate_tape(trades, now)
        self.assertEqual(w.trade_count_1m, 2)
        self.assertAlmostEqual(w.buy_notional_1m, 1.5)
        self.assertAlmostEqual(w.sell_notional_1m, 0.4)


class EvaluateTests(unittest.TestCase):
    def setUp(self):
        self.params = PumpPaperParams()
        self.now = 1_700_000_000_000

    def test_entry_all_conditions(self):
        sig = evaluate(
            snapshot=_snap(),
            tape=_hot_tape(),
            params=self.params,
            now_ms=self.now,
            impact_entry_bps=75.0,
        )
        self.assertEqual(sig.side, "long")
        self.assertEqual(sig.reason, "pump_paper_v1_entry")

    def test_rejects_complete_and_band(self):
        sig = evaluate(
            snapshot=_snap(complete=True, progress_bps=10_000, phase="graduating"),
            tape=_hot_tape(),
            params=self.params,
            now_ms=self.now,
            impact_entry_bps=80.0,
        )
        self.assertEqual(sig.side, "flat")
        self.assertEqual(sig.reason, "not_curve")

        sig = evaluate(
            snapshot=_snap(progress_bps=200),
            tape=_hot_tape(),
            params=self.params,
            now_ms=self.now,
            impact_entry_bps=80.0,
        )
        self.assertEqual(sig.reason, "progress_band")

        sig = evaluate(
            snapshot=_snap(progress_bps=1199),
            tape=_hot_tape(),
            params=self.params,
            now_ms=self.now,
            impact_entry_bps=40.0,
        )
        self.assertEqual(sig.reason, "progress_band")

        sig = evaluate(
            snapshot=_snap(progress_bps=1200),
            tape=_hot_tape(),
            params=self.params,
            now_ms=self.now,
            impact_entry_bps=40.0,
        )
        self.assertEqual(sig.side, "long")

        sig = evaluate(
            snapshot=_snap(progress_bps=6500),
            tape=_hot_tape(),
            params=self.params,
            now_ms=self.now,
            impact_entry_bps=40.0,
        )
        self.assertEqual(sig.side, "long")

        sig = evaluate(
            snapshot=_snap(progress_bps=6501),
            tape=_hot_tape(),
            params=self.params,
            now_ms=self.now,
            impact_entry_bps=40.0,
        )
        self.assertEqual(sig.reason, "progress_band")

    def test_momentum_and_impact(self):
        cold = TapeWindow(buy_notional_1m=1.0, sell_notional_1m=1.0, trade_count_1m=12)
        sig = evaluate(
            snapshot=_snap(),
            tape=cold,
            params=self.params,
            now_ms=self.now,
            impact_entry_bps=80.0,
        )
        self.assertEqual(sig.reason, "momentum")

        sig = evaluate(
            snapshot=_snap(),
            tape=_hot_tape(),
            params=self.params,
            now_ms=self.now,
            impact_entry_bps=400.0,
        )
        self.assertEqual(sig.reason, "impact")
        self.assertIn("SLIPPAGE_CAP", sig.tags)

        # Buffer: 75 passes, anything above the default buffer rejects.
        at_buffer = evaluate(
            snapshot=_snap(),
            tape=_hot_tape(),
            params=self.params,
            now_ms=self.now,
            impact_entry_bps=75.0,
        )
        self.assertEqual(at_buffer.side, "long")
        over_buffer = evaluate(
            snapshot=_snap(),
            tape=_hot_tape(),
            params=self.params,
            now_ms=self.now,
            impact_entry_bps=75.1,
        )
        self.assertEqual(over_buffer.reason, "impact")

        # Hard max 80: equal is allowed when the param is 80; above 80 always rejects.
        at_hard = PumpPaperParams(max_impact_bps=80.0)
        allowed = evaluate(
            snapshot=_snap(),
            tape=_hot_tape(),
            params=at_hard,
            now_ms=self.now,
            impact_entry_bps=80.0,
        )
        self.assertEqual(allowed.side, "long")
        over_hard = evaluate(
            snapshot=_snap(),
            tape=_hot_tape(),
            params=PumpPaperParams(max_impact_bps=200.0),
            now_ms=self.now,
            impact_entry_bps=80.1,
        )
        self.assertEqual(over_hard.reason, "impact")
        self.assertIn("GROSS_IMPACT_HARD", over_hard.tags)
        self.assertIn("SLIPPAGE_CAP", over_hard.tags)

    def test_forbidden_tags_and_cooldown(self):
        sig = evaluate(
            snapshot=_snap(),
            tape=_hot_tape(),
            params=self.params,
            now_ms=self.now,
            impact_entry_bps=75.0,
            extra_tags=["HONEYPOT"],
        )
        self.assertEqual(sig.reason, "blocked_tag")

        sig = evaluate(
            snapshot=_snap(),
            tape=_hot_tape(),
            params=self.params,
            now_ms=self.now,
            impact_entry_bps=75.0,
            last_open_ts=self.now - 10_000,
        )
        self.assertEqual(sig.reason, "cooldown")

    def test_exits_tp_sl_grad_hold(self):
        pos = PositionState(
            mint="m",
            symbol="PUMPDEMO/SOL",
            qty=1000,
            entry_price=1e-5,
            entry_ts=self.now - 1_000,
            entry_notional=0.1,
        )
        # price_sol 2.8e-5 vs entry 1e-5 → +180%
        sig = evaluate(
            snapshot=_snap(),
            tape=_hot_tape(),
            params=self.params,
            now_ms=self.now,
            impact_entry_bps=80.0,
            position=pos,
        )
        self.assertEqual(sig.reason, "take_profit")

        pos.entry_price = 0.001
        sig = evaluate(
            snapshot=_snap(),
            tape=_hot_tape(),
            params=self.params,
            now_ms=self.now,
            impact_entry_bps=80.0,
            position=pos,
        )
        self.assertEqual(sig.reason, "stop_loss")

        pos.entry_price = 2.8e-5
        sig = evaluate(
            snapshot=_snap(progress_bps=9200),
            tape=_hot_tape(),
            params=self.params,
            now_ms=self.now,
            impact_entry_bps=80.0,
            position=pos,
        )
        self.assertEqual(sig.reason, "graduation")

        sig = evaluate(
            snapshot=_snap(),
            tape=_hot_tape(),
            params=self.params,
            now_ms=self.now,
            impact_entry_bps=80.0,
            impact_exit_bps=400.0,
            position=pos,
        )
        self.assertEqual(sig.reason, "impact_split")
        self.assertIn("SPLIT_REDUCE", sig.tags)

        sig = evaluate(
            snapshot=_snap(),
            tape=_hot_tape(),
            params=self.params,
            now_ms=self.now + 1_000_000,
            impact_entry_bps=80.0,
            position=pos,
        )
        self.assertEqual(sig.reason, "max_hold")


class EngineAsyncTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        os.environ["DATA_PROVIDER"] = "pumpfun_paper"
        os.environ["PUMP_PAPER_LOOP"] = "0"
        os.environ["PUMPFUN_DISCOVERY"] = "off"
        reset_provider()
        reset_engine()
        reset_risk_gate()
        reset_paper_broker()

    def tearDown(self):
        reset_engine()
        reset_risk_gate()
        reset_paper_broker()
        os.environ["DATA_PROVIDER"] = "mock"
        reset_provider()

    def _seed_buy_tape(self, symbol: str = "PUMPDEMO/SOL") -> None:
        from app.providers import get_provider

        p = get_provider()
        now = int(time.time() * 1000)
        rows = []
        for i in range(12):
            rows.append(
                {
                    "mint": "DemoMintPump11111111111111111111111111111",
                    "symbol": symbol,
                    "ts": now - i * 1_000,
                    "side": "buy" if i < 10 else "sell",
                    "price": 2.8e-5,
                    "qty": 1000,
                    "sol_amount": 0.8 if i < 10 else 0.05,
                    "phase": "curve",
                }
            )
        p._trades[symbol] = rows  # type: ignore[attr-defined]

    async def test_auto_false_emits_signal_no_position(self):
        self._seed_buy_tape()
        engine = PumpPaperEngine(
            PumpPaperParams(auto_paper_orders=False, max_notional_sol=0.1)
        )
        await engine.tick()
        sig = engine.last_signal("PUMPDEMO/SOL")
        self.assertIsNotNone(sig)
        self.assertEqual(sig.side, "long")  # type: ignore[union-attr]
        self.assertEqual(engine.positions, {})
        hist = engine.history_for("PUMPDEMO/SOL")
        self.assertTrue(any(e.signal.side == "long" for e in hist))

    async def test_auto_true_opens_paper_fill(self):
        self._seed_buy_tape()
        engine = PumpPaperEngine(
            PumpPaperParams(auto_paper_orders=True, max_notional_sol=0.1)
        )
        await engine.tick()
        self.assertIn("PUMPDEMO/SOL", engine.positions)
        pos = engine.positions["PUMPDEMO/SOL"]
        self.assertGreater(pos.qty, 0)

    async def test_halt_blocks_open(self):
        from app.risk import get_risk_gate

        self._seed_buy_tape()
        gate = get_risk_gate()
        gate._trading_state = "halted"
        engine = PumpPaperEngine(
            PumpPaperParams(auto_paper_orders=True, max_notional_sol=0.1)
        )
        await engine.tick()
        # long marker still ok; no position
        sig = engine.last_signal("PUMPDEMO/SOL")
        self.assertIsNotNone(sig)
        self.assertEqual(engine.positions, {})

    async def test_orphan_position_exits_on_max_hold(self):
        """Held mint dropped from the watch list still flats after max_hold_sec.

        No live snapshot: tick synthesizes a paper mark and closes the position.
        liveEnabled stays false; no chain send.
        """
        from unittest.mock import patch

        from app.live.gate import live_enabled
        from app.live.send import send_wired
        from app.paper.ledger import get_paper_ledger, reset_paper_ledger
        from app.providers import get_provider

        self.assertFalse(live_enabled())
        self.assertFalse(send_wired())
        reset_paper_ledger()

        self._seed_buy_tape()
        engine = PumpPaperEngine(
            PumpPaperParams(auto_paper_orders=True, max_notional_sol=0.1, max_hold_sec=900)
        )
        await engine.tick()
        self.assertIn("PUMPDEMO/SOL", engine.positions)
        pos = engine.positions["PUMPDEMO/SOL"]
        self.assertGreater(pos.qty, 0)

        provider = get_provider()
        with provider._lock:  # type: ignore[attr-defined]
            provider._drop_locked(pos.symbol, pos.mint)  # type: ignore[attr-defined]
        engine._last_snap.pop(pos.symbol, None)

        listed = {s.symbol for s in provider.list_symbols()}
        self.assertNotIn("PUMPDEMO/SOL", listed)
        self.assertIsNone(provider.get_pumpfun_snapshot("PUMPDEMO/SOL"))
        covered = engine._symbols_for_tick(provider)
        self.assertTrue(listed.issubset(set(covered)))
        self.assertIn("PUMPDEMO/SOL", covered)
        self.assertEqual(set(covered), listed | set(engine.positions))

        future = time.time() + float(engine.params.max_hold_sec) + 5.0
        with patch("app.strategies.pump_paper_v1.time.time", return_value=future):
            await engine.tick()

        self.assertNotIn("PUMPDEMO/SOL", engine.positions)
        sig = engine.last_signal("PUMPDEMO/SOL")
        self.assertIsNotNone(sig)
        self.assertEqual(sig.side, "flat")  # type: ignore[union-attr]
        self.assertEqual(sig.reason, "max_hold")  # type: ignore[union-attr]
        self.assertIn("ORPHAN_EXIT", sig.tags)  # type: ignore[union-attr]
        closed = [t for t in get_paper_ledger().closed if t.symbol == "PUMPDEMO/SOL"]
        self.assertEqual(len(closed), 1)
        self.assertIn("MAX_HOLD", closed[0].tags)
        self.assertFalse(live_enabled())
        self.assertFalse(send_wired())


class ApiStrategyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["DATA_PROVIDER"] = "pumpfun_paper"
        os.environ["PUMP_PAPER_LOOP"] = "0"
        os.environ["PUMPFUN_DISCOVERY"] = "off"
        reset_provider()
        reset_engine()
        reset_risk_gate()
        from fastapi.testclient import TestClient
        from app.main import app

        cls.client = TestClient(app)

    @classmethod
    def tearDownClass(cls):
        os.environ["DATA_PROVIDER"] = "mock"
        os.environ["PUMP_PAPER_LOOP"] = "1"
        reset_provider()
        reset_engine()
        reset_risk_gate()

    def test_config_defaults_auto_false(self):
        r = self.client.get("/api/v1/strategy/pump-paper-v1")
        self.assertEqual(r.status_code, 200)
        data = r.json()["data"]
        self.assertFalse(data["params"]["auto_paper_orders"])
        self.assertFalse(data["auto_paper_orders"])
        self.assertFalse(data["strategy_autopaper"])
        self.assertEqual(data["params"]["progress_bps_min"], 1200)
        self.assertEqual(data["params"]["progress_bps_max"], 6500)
        self.assertEqual(data["params"]["max_impact_bps"], 75)
        self.assertAlmostEqual(data["params"]["notional_pct_equity"], 0.005)
        self.assertAlmostEqual(data["params"]["take_profit_pct"], 0.10)
        self.assertAlmostEqual(data["params"]["stop_loss_pct"], 0.07)
        self.assertEqual(data["params"]["max_hold_sec"], 300)
        self.assertEqual(data["params"]["max_notional_sol"], 0.12)
        raised = self.client.put(
            "/api/v1/strategy/pump-paper-v1", json={"max_impact_bps": 200}
        )
        self.assertLessEqual(raised.json()["data"]["params"]["max_impact_bps"], 80.0)
        self.client.put("/api/v1/strategy/pump-paper-v1", json={"max_impact_bps": 75})
        self.assertIn(data["trading_state"], ("active", "reducing", "halted"))
        live = self.client.get("/api/v1/live/status")
        self.assertEqual(live.status_code, 200)
        self.assertFalse(live.json()["data"].get("liveEnabled", False))

    def test_put_auto_toggle(self):
        r = self.client.put(
            "/api/v1/strategy/pump-paper-v1", json={"auto_paper_orders": True}
        )
        self.assertTrue(r.json()["data"]["auto_paper_orders"])
        r = self.client.post(
            "/api/v1/strategy/pump-paper-v1", json={"auto_paper_orders": False}
        )
        self.assertFalse(r.json()["data"]["auto_paper_orders"])

    def test_put_strategy_autopaper_alias(self):
        r = self.client.put(
            "/api/v1/strategy/pump-paper-v1", json={"strategy_autopaper": True}
        )
        self.assertTrue(r.json()["data"]["auto_paper_orders"])
        self.assertTrue(r.json()["data"]["strategy_autopaper"])
        r = self.client.put(
            "/api/v1/strategy/pump-paper-v1", json={"strategy_autopaper": False}
        )
        self.assertFalse(r.json()["data"]["auto_paper_orders"])

    def test_health_and_monitor(self):
        h = self.client.get("/api/v1/health")
        self.assertIn("auto_paper_orders", h.json()["data"])
        self.assertIn("strategy_autopaper", h.json()["data"])
        self.assertEqual(
            h.json()["data"]["auto_paper_orders"], h.json()["data"]["strategy_autopaper"]
        )
        self.assertIn("trading_state", h.json()["data"])
        m = self.client.get("/api/v1/pumpfun/monitor")
        self.assertEqual(m.status_code, 200)
        rows = m.json()["data"]
        self.assertTrue(any(x["symbol"] == "PUMPDEMO/SOL" for x in rows))
        row = next(x for x in rows if x["symbol"] == "PUMPDEMO/SOL")
        self.assertIsNotNone(row["progress_bps"])
        self.assertIn("buy_notional_1m", row)
        self.assertIn("sell_notional_1m", row)
        self.assertIn("tags", row)

    def test_day_loss_halt_blocks_pre_order(self):
        reset_risk_gate()
        ctx = {
            "symbol": "PUMPDEMO/SOL",
            "ts": 1_700_000_000_000,
            "account": {"equity": 10_000.0, "day_pnl": 0.0},
            "tick": {"mid": 0.00003},
            "liquidity": {"spread_bps": 20, "adv_usd": 100000},
        }
        fill = {
            "ts": 1,
            "price": 1.0,
            "qty": 1.0,
            "fee": 600.0,
            "slippage_bps": 1,
            "tag": "paper",
        }
        pf = self.client.post("/api/v1/risk/post-fill", json={"ctx": ctx, "fill": fill})
        self.assertEqual(pf.status_code, 200)
        self.assertEqual(pf.json()["data"]["trading_state"], "halted")
        pre = self.client.post(
            "/api/v1/risk/pre-order",
            json={
                "ctx": ctx,
                "signal": {"side": "long", "reason": "test"},
                "size": {"target_notional": 0.1, "max_slippage_bps": 500},
            },
        )
        risk = pre.json()["data"]
        self.assertFalse(risk["allow"])
        self.assertIn("TRADING_HALTED", risk["tags"])
        reset_risk_gate()


if __name__ == "__main__":
    unittest.main()
