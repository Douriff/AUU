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
    IMPACT_HARD_CAP_BPS,
    PAPER_ENTRY_IMPACT_BUDGET_BPS,
    PAPER_IMPACT_FEE_BPS,
    PAPER_MAX_NOTIONAL_SOL,
    PositionState,
    PumpPaperEngine,
    PumpPaperParams,
    TapeWindow,
    aggregate_tape,
    curve_impact_bps,
    entry_impact_budget,
    evaluate,
    fit_notional,
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
        self.assertEqual(p.progress_bps_min, 800)
        self.assertEqual(p.progress_bps_max, 7500)
        self.assertEqual(p.max_impact_bps, 80.0)
        self.assertAlmostEqual(p.notional_pct_equity, 0.005)
        self.assertFalse(p.auto_paper_orders)
        self.assertAlmostEqual(p.max_notional_sol, PAPER_MAX_NOTIONAL_SOL)
        self.assertAlmostEqual(p.entry_impact_budget_bps, PAPER_ENTRY_IMPACT_BUDGET_BPS)
        self.assertAlmostEqual(p.impact_fee_bps, PAPER_IMPACT_FEE_BPS)
        self.assertLess(p.entry_impact_budget_bps, 60.0)
        self.assertLessEqual(p.max_impact_bps, IMPACT_HARD_CAP_BPS)

    def test_hard_cap_cannot_be_raised(self):
        p = PumpPaperParams(max_impact_bps=400, entry_impact_budget_bps=200)
        self.assertEqual(p.max_impact_bps, IMPACT_HARD_CAP_BPS)
        self.assertEqual(p.entry_impact_budget_bps, IMPACT_HARD_CAP_BPS)
        self.assertEqual(entry_impact_budget(p), IMPACT_HARD_CAP_BPS)

    def test_live_stays_off(self):
        from app.live.gate import evaluate as live_evaluate
        from app.paper.executability import LIVE_ENABLED

        self.assertFalse(LIVE_ENABLED)
        self.assertFalse(live_evaluate().live_enabled)


class PaperEntryImpactTests(unittest.TestCase):
    """Default paper clip targets executability median < 60 without fake fills."""

    def _at(self, progress: int) -> PumpfunPaperSnapshot:
        from app.providers.pumpfun_curve_math import price_sol, reserves_at_progress_bps

        vs, vt, rs, rt = reserves_at_progress_bps(progress)
        return _snap(
            progress_bps=progress,
            virtual_sol_reserves=str(vs),
            virtual_token_reserves=str(vt),
            real_sol_reserves=str(rs),
            real_token_reserves=str(rt),
            price_sol=price_sol(vs, vt),
        )

    def test_smaller_notional_lower_curve_impact(self):
        snap = self._at(4200)
        params = PumpPaperParams()
        big = curve_impact_bps(snap, 0.05, "buy", fee_bps=params.impact_fee_bps)
        small = curve_impact_bps(snap, params.max_notional_sol, "buy", fee_bps=params.impact_fee_bps)
        self.assertLess(params.max_notional_sol, 0.05)
        self.assertLess(small, big)

    def test_default_band_median_under_60(self):
        params = PumpPaperParams()
        impacts: list[float] = []
        for progress in range(800, 7501, 50):
            snap = self._at(progress)
            sized = fit_notional(snap, 10_000.0, params, "buy")
            self.assertIsNotNone(sized)
            assert sized is not None
            self.assertLessEqual(sized, params.max_notional_sol)
            impact = curve_impact_bps(snap, sized, "buy", fee_bps=params.impact_fee_bps)
            self.assertLessEqual(impact, entry_impact_budget(params))
            self.assertLessEqual(impact, IMPACT_HARD_CAP_BPS)
            impacts.append(impact)
        impacts.sort()
        mid = impacts[len(impacts) // 2]
        self.assertLess(mid, 60.0)
        self.assertLessEqual(max(impacts), 80.0)
        # Old clip (0.5 SOL, library fee 125, sized up to the 80 cap) sits near ~78.
        legacy = curve_impact_bps(self._at(4200), 0.03125, "buy", fee_bps=125)
        self.assertGreater(legacy, 70.0)
        self.assertLess(mid, legacy)

    def test_over_budget_curve_does_not_fall_back_to_large_clip(self):
        """A quote that cannot meet the budget returns None instead of the 0.5 SOL clip."""
        params = PumpPaperParams(max_notional_sol=0.5, impact_fee_bps=125, entry_impact_budget_bps=55)
        sized = fit_notional(self._at(4200), 10_000.0, params, "buy")
        self.assertIsNone(sized)


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
            impact_entry_bps=80.0,
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
            snapshot=_snap(progress_bps=799),
            tape=_hot_tape(),
            params=self.params,
            now_ms=self.now,
            impact_entry_bps=40.0,
        )
        self.assertEqual(sig.reason, "progress_band")

        sig = evaluate(
            snapshot=_snap(progress_bps=800),
            tape=_hot_tape(),
            params=self.params,
            now_ms=self.now,
            impact_entry_bps=40.0,
        )
        self.assertEqual(sig.side, "long")

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

    def test_forbidden_tags_and_cooldown(self):
        sig = evaluate(
            snapshot=_snap(),
            tape=_hot_tape(),
            params=self.params,
            now_ms=self.now,
            impact_entry_bps=80.0,
            extra_tags=["HONEYPOT"],
        )
        self.assertEqual(sig.reason, "blocked_tag")

        sig = evaluate(
            snapshot=_snap(),
            tape=_hot_tape(),
            params=self.params,
            now_ms=self.now,
            impact_entry_bps=80.0,
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

    async def test_default_clip_fill_impact_under_60(self):
        from app.paper.decision_log import get_decision_log, reset_decision_log

        reset_decision_log()
        self._seed_buy_tape()
        engine = PumpPaperEngine(PumpPaperParams(auto_paper_orders=True))
        self.assertAlmostEqual(engine.params.max_notional_sol, 0.01)
        await engine.tick()
        self.assertIn("PUMPDEMO/SOL", engine.positions)
        fills = [r for r in get_decision_log().all() if r.outcome in {"fill", "partial"}]
        self.assertTrue(fills)
        impact = fills[-1].impact_bps_est
        self.assertIsNotNone(impact)
        assert impact is not None
        self.assertLess(impact, 60.0)
        self.assertLessEqual(impact, 80.0)
        self.assertLessEqual(float(fills[-1].notional_sol or 0.0), 0.01 + 1e-9)

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
        self.assertEqual(data["params"]["progress_bps_min"], 800)
        self.assertEqual(data["params"]["progress_bps_max"], 7500)
        self.assertEqual(data["params"]["max_impact_bps"], 80)
        self.assertAlmostEqual(data["params"]["notional_pct_equity"], 0.005)
        self.assertAlmostEqual(data["params"]["max_notional_sol"], 0.01)
        self.assertAlmostEqual(data["params"]["entry_impact_budget_bps"], 55)
        self.assertAlmostEqual(data["params"]["impact_fee_bps"], 100)
        self.assertTrue(data["liveDisabled"])
        self.assertIn(data["trading_state"], ("active", "reducing", "halted"))

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
