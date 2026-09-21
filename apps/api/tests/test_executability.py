"""Executability aggregator — paper evidence only; live stays disabled."""
from __future__ import annotations

import json
import os
import unittest
from pathlib import Path

from app.models.contracts import Fill, OrderIntent, StrategyContext, TickCtx
from app.paper.executability import (
    IMPACT_HARD_MAX_BPS,
    IMPACT_MEDIAN_MAX_BPS,
    LIVE_ENABLED,
    LIVE_LIMITS,
    MIN_CLOSED_TRADES,
    SHADOW_SLIPPAGE_X_BPS,
    aggregate_executability,
    annotate_fill_executability,
    classify_reject_reason,
    shadow_slippage_bps,
)
from app.paper.ledger import PaperTradeJournal, reset_paper_ledger
from app.paper.pipeline import run_paper_order
from app.risk.gate import reset_risk_gate
from app.strategies.pump_paper_v1 import PumpPaperEngine, PumpPaperParams, reset_engine

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "executability"
ROOT = Path(__file__).resolve().parents[3]


def _load(name: str) -> dict:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def _expand_trades(recipe: dict) -> list[dict]:
    n = int(recipe["n"])
    rows: list[dict] = []
    for i in range(n):
        rows.append(
            {
                "id": f"rt-{i}",
                "strategy_id": "pump-paper-v1",
                "symbol": "PUMPDEMO/SOL",
                "mint": None,
                "entry_ts": 1_000 + i,
                "exit_ts": 2_000 + i,
                "entry_price": float(recipe.get("entry_price", 1.0)),
                "exit_price": 1.05,
                "qty": 1.0,
                "pnl": float(recipe["pnl"]),
                "pnl_pct": 0.05,
                "fees": 0.0,
                "tags": ["pump-paper-v1"],
                "source": "signal",
                "side": "long",
                "entry_estimated_impact_bps": float(recipe["entry_estimated_impact_bps"]),
                "entry_quote_price": float(recipe.get("entry_quote_price", 1.0)),
                "entry_shadow_slippage_bps": float(recipe["entry_shadow_slippage_bps"]),
            }
        )
    return rows


class ClassifyTests(unittest.TestCase):
    def test_buckets(self):
        self.assertEqual(classify_reject_reason("progress_band"), "progress_band")
        self.assertEqual(classify_reject_reason("not_curve"), "progress_band")
        self.assertEqual(classify_reject_reason("impact"), "impact")
        self.assertEqual(classify_reject_reason("pump_paper_v1_entry", ["SLIPPAGE_CAP"]), "impact")
        self.assertEqual(classify_reject_reason("blocked_tag", ["HONEYPOT_FLAG"]), "risk")
        self.assertEqual(classify_reject_reason("LIVE_DISABLED", ["LIVE_DISABLED"]), "risk")
        self.assertIsNone(classify_reject_reason("auto_paper_orders=false", ["AUTOPAPER_OFF"]))
        self.assertIsNone(classify_reject_reason("momentum"))


class ShadowMathTests(unittest.TestCase):
    def test_quote_vs_fill(self):
        self.assertAlmostEqual(shadow_slippage_bps(1.004, 1.0), 40.0)
        self.assertIsNone(shadow_slippage_bps(1.0, 0.0))


class FixtureAggregatorTests(unittest.TestCase):
    def test_go_30(self):
        recipe = _load("go_30.json")
        data = aggregate_executability(
            _expand_trades(recipe), eval_counts=recipe["eval_counts"]
        )
        self.assertEqual(data["verdict"], "go")
        self.assertTrue(data["sample_ok"])
        self.assertEqual(data["n_trades"], MIN_CLOSED_TRADES)
        self.assertGreaterEqual(data["expectancy"], 0)
        self.assertLess(data["median_entry_impact_bps"], IMPACT_MEDIAN_MAX_BPS)
        self.assertLessEqual(data["max_entry_impact_bps"], IMPACT_HARD_MAX_BPS)
        self.assertTrue(data["shadow_slippage"]["ok"])
        self.assertLessEqual(data["shadow_slippage"]["median_bps"], SHADOW_SLIPPAGE_X_BPS)
        for key in ("progress_band", "impact", "risk"):
            self.assertIn(key, data["reject_rate"])
            self.assertIn("count", data["reject_rate"][key])
            self.assertIn("rate", data["reject_rate"][key])
        self.assertFalse(data["liveEnabled"])
        self.assertTrue(data["liveDisabled"])
        self.assertFalse(data["gates"]["live"]["ok"])
        self.assertEqual(data["live_limits"], LIVE_LIMITS)
        self.assertEqual(data["hard_max_impact_bps"], 80.0)

    def test_nogo_sample(self):
        recipe = _load("nogo_sample.json")
        data = aggregate_executability(
            _expand_trades(recipe), eval_counts=recipe["eval_counts"]
        )
        self.assertEqual(data["verdict"], "no-go")
        self.assertFalse(data["sample_ok"])
        self.assertFalse(data["gates"]["sample_ok"]["ok"])
        self.assertFalse(data["liveEnabled"])

    def test_nogo_expectancy(self):
        recipe = _load("nogo_expectancy.json")
        data = aggregate_executability(
            _expand_trades(recipe), eval_counts=recipe["eval_counts"]
        )
        self.assertEqual(data["verdict"], "no-go")
        self.assertTrue(data["sample_ok"])
        self.assertFalse(data["gates"]["expectancy"]["ok"])
        self.assertLess(data["expectancy"], 0)

    def test_nogo_median_impact(self):
        recipe = _load("nogo_impact.json")
        data = aggregate_executability(
            _expand_trades(recipe), eval_counts=recipe["eval_counts"]
        )
        self.assertEqual(data["verdict"], "no-go")
        self.assertFalse(data["gates"]["median_entry_impact"]["ok"])
        self.assertGreaterEqual(data["median_entry_impact_bps"], IMPACT_MEDIAN_MAX_BPS)
        self.assertLessEqual(data["max_entry_impact_bps"], IMPACT_HARD_MAX_BPS)

    def test_nogo_shadow(self):
        recipe = _load("nogo_shadow.json")
        data = aggregate_executability(
            _expand_trades(recipe), eval_counts=recipe["eval_counts"]
        )
        self.assertEqual(data["verdict"], "no-go")
        self.assertFalse(data["gates"]["shadow_slippage"]["ok"])
        self.assertGreater(data["shadow_slippage"]["median_bps"], SHADOW_SLIPPAGE_X_BPS)

    def test_hard_max_impact_breach(self):
        recipe = _load("go_30.json")
        trades = _expand_trades(recipe)
        trades[0]["entry_estimated_impact_bps"] = 90.0
        data = aggregate_executability(trades, eval_counts=recipe["eval_counts"])
        self.assertEqual(data["verdict"], "no-go")
        self.assertFalse(data["gates"]["median_entry_impact"]["ok"])
        self.assertGreater(data["max_entry_impact_bps"], IMPACT_HARD_MAX_BPS)

    def test_reject_rate_from_decision_fixture(self):
        raw = _load("decisions_mix.json")
        data = aggregate_executability([], decisions=raw["decisions"])
        self.assertEqual(data["n_trades"], 0)
        self.assertEqual(data["verdict"], "no-go")
        by = data["reject_rate"]
        self.assertEqual(by["progress_band"]["count"], 2)
        self.assertEqual(by["impact"]["count"], 1)
        self.assertEqual(by["risk"]["count"], 1)
        self.assertTrue(data["gates"]["reject_rate"]["ok"])
        # autopaper-off skip is ignored
        self.assertNotIn("auto_paper_orders=false", data["reject_reasons"])
        self.assertFalse(data["liveEnabled"])

    def test_live_limits_not_weakened(self):
        self.assertFalse(LIVE_ENABLED)
        self.assertEqual(LIVE_LIMITS["max_notional_sol"], 1.0)
        self.assertEqual(LIVE_LIMITS["max_day_loss_pct"], 0.045)
        self.assertEqual(LIVE_LIMITS["max_open_mints"], 10)
        # raising any of these would weaken the live cap
        self.assertLessEqual(LIVE_LIMITS["max_notional_sol"], 1.0)
        self.assertLessEqual(LIVE_LIMITS["max_day_loss_pct"], 0.045)
        self.assertLessEqual(LIVE_LIMITS["max_open_mints"], 10)


class AnnotateFillTests(unittest.TestCase):
    def test_shadow_from_mid(self):
        ctx = StrategyContext(symbol="PUMPDEMO/SOL", ts=1, tick=TickCtx(mid=1.0))
        intent = OrderIntent(side="buy", qty_or_notional=0.1)
        fill = Fill(ts=1, price=1.002, qty=0.1)
        out = annotate_fill_executability(fill, ctx, intent)
        self.assertAlmostEqual(out.quote_price, 1.0)
        self.assertAlmostEqual(out.shadow_slippage_bps, 20.0)
        self.assertIsNotNone(out.estimated_impact_bps)
        self.assertEqual(fill.price, 1.002)


class LedgerExecFieldsTests(unittest.TestCase):
    def test_round_trip_copies_entry_impact(self):
        led = PaperTradeJournal()
        buy = Fill(
            ts=1,
            price=1.0,
            qty=2.0,
            estimated_impact_bps=41.0,
            quote_price=0.999,
            shadow_slippage_bps=10.0,
            tag="paper:pump-paper-v1",
        )
        led.record_fill("A/SOL", buy)
        led.record_fill(
            "A/SOL",
            Fill(
                ts=2,
                price=1.1,
                qty=-2.0,
                estimated_impact_bps=20.0,
                quote_price=1.1,
                shadow_slippage_bps=0.0,
                tag="paper:pump-paper-v1:flat",
            ),
        )
        rt = led.closed[0]
        self.assertAlmostEqual(rt.entry_estimated_impact_bps, 41.0)
        self.assertAlmostEqual(rt.entry_shadow_slippage_bps, 10.0)
        dumped = rt.as_dict()
        self.assertIn("entry_estimated_impact_bps", dumped)


class EngineEvalCountTests(unittest.TestCase):
    def test_record_entry_eval_buckets(self):
        from app.models.contracts import SignalOut

        eng = PumpPaperEngine(PumpPaperParams(auto_paper_orders=False))
        eng.record_entry_eval(SignalOut(side="flat", reason="progress_band"))
        eng.record_entry_eval(SignalOut(side="flat", reason="impact", tags=["SLIPPAGE_CAP"]))
        eng.record_entry_eval(SignalOut(side="long", reason="pump_paper_v1_entry"))
        snap = eng.eval_snapshot()
        self.assertEqual(snap["total"], 3)
        self.assertEqual(snap["by_bucket"]["progress_band"], 1)
        self.assertEqual(snap["by_bucket"]["impact"], 1)
        self.assertEqual(snap["by_bucket"]["attempt"], 1)


class NoChainSendTests(unittest.TestCase):
    def test_modules_have_no_send(self):
        files = [
            ROOT / "apps" / "api" / "app" / "paper" / "executability.py",
            ROOT / "apps" / "api" / "app" / "routes" / "stats.py",
        ]
        for path in files:
            text = path.read_text(encoding="utf-8").lower()
            self.assertNotIn("sendtransaction", text)
            self.assertNotIn("sniper", text)
            self.assertNotIn("private_key", text)


class ExecutabilityApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["DATA_PROVIDER"] = "pumpfun_paper"
        os.environ["PUMP_PAPER_LOOP"] = "0"
        os.environ["PUMPFUN_DISCOVERY"] = "off"
        reset_engine()
        reset_risk_gate()
        reset_paper_ledger()
        from fastapi.testclient import TestClient
        from app.main import app

        cls.client = TestClient(app)

    @classmethod
    def tearDownClass(cls):
        os.environ["DATA_PROVIDER"] = "mock"
        os.environ["PUMP_PAPER_LOOP"] = "1"
        reset_engine()
        reset_risk_gate()
        reset_paper_ledger()

    def setUp(self):
        reset_paper_ledger()
        reset_engine()
        reset_risk_gate()

    def test_empty_endpoint_is_nogo_live_false(self):
        r = self.client.get("/api/v1/stats/executability")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["ok"])
        data = body["data"]
        self.assertEqual(data["verdict"], "no-go")
        self.assertFalse(data["liveEnabled"])
        self.assertTrue(data["liveDisabled"])
        self.assertFalse(data["gates"]["live"]["ok"])
        self.assertIn("liveEnabled", data["gates"]["live"]["reason"])
        self.assertIn("LiveLimits", data["gates"]["live"]["reason"])
        self.assertEqual(data["live_limits"]["max_notional_sol"], 1.0)
        self.assertEqual(data["hard_max_impact_bps"], 80.0)
        self.assertIn("progress_band", data["reject_rate"])
        self.assertIn("impact", data["reject_rate"])
        self.assertIn("risk", data["reject_rate"])
        self.assertNotIn("secret", json.dumps(data).lower())
        self.assertNotIn("keypair", json.dumps(data).lower())

    def test_pipeline_fill_carries_exec_fields(self):
        buy = self.client.post(
            "/api/v1/pipeline/decide-and-fill",
            json={"symbol": "PUMPDEMO/SOL", "side": "buy", "notional": 0.05},
        )
        self.assertEqual(buy.status_code, 200)
        fills = buy.json()["data"]["fills"]
        self.assertGreaterEqual(len(fills), 1)
        f = fills[0]
        self.assertIn("quote_price", f)
        self.assertIsNotNone(f.get("estimated_impact_bps"))
        self.assertIsNotNone(f.get("shadow_slippage_bps"))
        self.client.post(
            "/api/v1/pipeline/decide-and-fill",
            json={"symbol": "PUMPDEMO/SOL", "side": "sell", "notional": 0.05},
        )
        r = self.client.get("/api/v1/stats/executability")
        data = r.json()["data"]
        self.assertGreaterEqual(data["n_trades"], 1)
        self.assertFalse(data["liveEnabled"])
        self.assertEqual(data["verdict"], "no-go")  # sample < 30

    def test_root_lists_endpoint(self):
        r = self.client.get("/")
        self.assertIn("executability", r.json()["data"]["endpoints"])


class PipelineNoSendTests(unittest.TestCase):
    def test_paper_order_path_still_broker_only(self):
        src = Path(run_paper_order.__code__.co_filename).read_text(encoding="utf-8")
        self.assertIn("get_paper_broker", src)
        self.assertNotIn("sendTransaction", src)


if __name__ == "__main__":
    unittest.main()
