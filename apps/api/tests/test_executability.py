"""Executability aggregator — paper evidence only; live stays disabled."""
from __future__ import annotations

import json
import os
import unittest
from pathlib import Path

from app.models.contracts import Fill, OrderIntent, StrategyContext, TickCtx
from app.paper.decision_log import (
    classify_reject_bucket,
    make_row,
    pick_shadow_fill_px,
    reset_decision_log,
    shadow_metrics,
    signed_shadow_slippage_bps,
)
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
from app.providers.pumpfun_curve_math import (
    AMM_IMPACT_FEE_FLOOR_BPS,
    CURVE_IMPACT_FEE_FLOOR_BPS,
    impact_net_bps,
    protocol_fee_bps_for_phase,
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
        self.assertEqual(classify_reject_bucket("progress_band"), "progress")
        self.assertEqual(classify_reject_reason("not_curve"), "progress")
        self.assertEqual(classify_reject_bucket("impact"), "impact")
        self.assertEqual(classify_reject_bucket("pump_paper_v1_entry", ["SLIPPAGE_CAP"]), "impact")
        self.assertEqual(classify_reject_bucket("x", ["DEPTH_THIN"]), "impact")
        self.assertEqual(classify_reject_bucket("blocked_tag", ["DAY_LOSS_BREAKER"]), "risk")
        self.assertEqual(classify_reject_bucket("LIVE_DISABLED", ["LIVE_DISABLED"]), "risk")
        self.assertEqual(classify_reject_bucket("auto_paper_orders=false", ["AUTOPAPER_OFF"]), "none")
        self.assertEqual(classify_reject_bucket("momentum"), "none")


class ShadowMathTests(unittest.TestCase):
    def test_quote_vs_fill(self):
        self.assertAlmostEqual(shadow_slippage_bps(1.004, 1.0), 40.0)
        self.assertIsNone(shadow_slippage_bps(1.0, 0.0))

    def test_next_trade_preferred_over_bar(self):
        px, src = pick_shadow_fill_px(
            100,
            trades=[{"ts": 50, "price": 1.0}, {"ts": 150, "price": 1.01}],
            candles=[{"t": 200, "o": 1.5}],
        )
        self.assertAlmostEqual(px, 1.01)
        self.assertEqual(src, "next_trade")

    def test_next_open_fallback(self):
        px, src = pick_shadow_fill_px(
            100,
            trades=[{"ts": 90, "price": 1.0}],
            candles=[{"t": 80, "o": 0.9}, {"t": 180, "o": 1.02}],
        )
        self.assertAlmostEqual(px, 1.02)
        self.assertEqual(src, "next_open")

    def test_impact_error_is_shadow_minus_estimated(self):
        fill = Fill(ts=1, price=1.0, qty=1.0)
        m = shadow_metrics(
            fill, 40.0, shadow_fill_px=1.005, source="next_trade", decision_px=1.0, side="buy"
        )
        self.assertAlmostEqual(m["shadow_slippage_bps"], 50.0)
        self.assertAlmostEqual(m["impact_error_bps"], 10.0)
        self.assertEqual(m["shadow_source"], "next_trade")
        self.assertEqual(m["estimated_impact_bps"], 40.0)
        self.assertAlmostEqual(m["decision_px"], 1.0)
        self.assertAlmostEqual(m["paper_fill_px"], 1.0)

    def test_sell_flips_shadow_sign(self):
        self.assertAlmostEqual(signed_shadow_slippage_bps("sell", 1.0, 0.99), 100.0)
        fill = Fill(ts=1, price=0.995, qty=-1.0)
        m = shadow_metrics(
            fill, 40.0, shadow_fill_px=0.99, source="next_trade", decision_px=1.0, side="sell"
        )
        self.assertAlmostEqual(m["shadow_slippage_bps"], 100.0)
        self.assertAlmostEqual(m["impact_error_bps"], 60.0)


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
        self.assertAlmostEqual(data["median_entry_impact_gross_bps"], 42.0)
        self.assertAlmostEqual(data["protocol_fee_bps"], CURVE_IMPACT_FEE_FLOOR_BPS)
        self.assertAlmostEqual(data["median_entry_impact_net_bps"], 0.0)
        self.assertLess(data["gates"]["median_entry_impact"]["median_net_bps"], IMPACT_MEDIAN_MAX_BPS)
        self.assertLessEqual(data["max_entry_impact_bps"], IMPACT_HARD_MAX_BPS)
        self.assertTrue(data["shadow_slippage"]["ok"])
        self.assertTrue(data["shadow_slippage"]["coverage_ok"])
        self.assertEqual(data["shadow_slippage"]["n"], MIN_CLOSED_TRADES)
        self.assertEqual(data["shadow_slippage"]["min_n"], MIN_CLOSED_TRADES)
        self.assertLessEqual(data["shadow_slippage"]["median_bps"], SHADOW_SLIPPAGE_X_BPS)
        self.assertIn("impact_error", data)
        self.assertFalse(data["liveEnabled"])
        for key in ("progress", "impact", "risk"):
            self.assertIn(key, data["reject_rate"])
            self.assertIn("count", data["reject_rate"][key])
            self.assertIn("rate", data["reject_rate"][key])
        self.assertEqual(data["lamp"], "green")
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
        """Gross ~150 still fails: net median stays ≥60 and gross breaches 80."""
        recipe = _load("nogo_impact.json")
        data = aggregate_executability(
            _expand_trades(recipe), eval_counts=recipe["eval_counts"]
        )
        self.assertEqual(data["verdict"], "no-go")
        self.assertFalse(data["gates"]["median_entry_impact"]["ok"])
        self.assertAlmostEqual(data["median_entry_impact_gross_bps"], 150.0)
        self.assertAlmostEqual(data["protocol_fee_bps"], CURVE_IMPACT_FEE_FLOOR_BPS)
        self.assertAlmostEqual(data["median_entry_impact_net_bps"], 87.5)
        self.assertGreaterEqual(data["median_entry_impact_net_bps"], IMPACT_MEDIAN_MAX_BPS)
        self.assertGreater(data["max_entry_impact_bps"], IMPACT_HARD_MAX_BPS)
        self.assertEqual(data["gates"]["median_entry_impact"]["basis"], "net_of_protocol_fee")
        self.assertFalse(data["liveEnabled"])

    def test_nogo_shadow(self):
        recipe = _load("nogo_shadow.json")
        data = aggregate_executability(
            _expand_trades(recipe), eval_counts=recipe["eval_counts"]
        )
        self.assertEqual(data["verdict"], "no-go")
        gate = data["gates"]["shadow_slippage"]
        self.assertFalse(gate["ok"])
        self.assertTrue(gate["coverage_ok"])
        self.assertEqual(gate["n"], MIN_CLOSED_TRADES)
        self.assertEqual(gate["min_n"], MIN_CLOSED_TRADES)
        self.assertGreater(data["shadow_slippage"]["median_bps"], SHADOW_SLIPPAGE_X_BPS)
        self.assertIn("P50", data["nogo_reason"])
        self.assertNotIn("shadow coverage", data["nogo_reason"])
        self.assertEqual(data["lamp"], "red")
        self.assertFalse(data["liveEnabled"])

    def test_net_of_fee_gross_76_goes(self):
        """Gross ~76 with curve fee 62.5 → net ~13.5 clears the <60 median."""
        recipe = _load("go_30.json")
        trades = _expand_trades(recipe)
        for row in trades:
            row["entry_estimated_impact_bps"] = 76.0
        data = aggregate_executability(trades, eval_counts=recipe["eval_counts"])
        self.assertAlmostEqual(impact_net_bps(76.0, CURVE_IMPACT_FEE_FLOOR_BPS), 13.5)
        self.assertAlmostEqual(data["protocol_fee_bps"], 62.5)
        self.assertAlmostEqual(data["median_entry_impact_gross_bps"], 76.0)
        self.assertAlmostEqual(data["median_entry_impact_net_bps"], 13.5)
        self.assertLess(data["median_entry_impact_net_bps"], IMPACT_MEDIAN_MAX_BPS)
        self.assertLessEqual(data["max_entry_impact_gross_bps"], IMPACT_HARD_MAX_BPS)
        self.assertTrue(data["gates"]["median_entry_impact"]["ok"])
        self.assertEqual(data["verdict"], "go")
        self.assertEqual(data["lamp"], "green")
        self.assertFalse(data["liveEnabled"])
        self.assertTrue(data["sample_ok"])
        self.assertGreaterEqual(data["n_trades"], MIN_CLOSED_TRADES)

    def test_sample_ok_still_requires_30_when_net_passes(self):
        recipe = _load("go_30.json")
        trades = _expand_trades(recipe)[:29]
        for row in trades:
            row["entry_estimated_impact_bps"] = 76.0
        data = aggregate_executability(trades, eval_counts=recipe["eval_counts"])
        self.assertEqual(len(trades), 29)
        self.assertAlmostEqual(data["median_entry_impact_net_bps"], 13.5)
        self.assertFalse(data["sample_ok"])
        self.assertFalse(data["gates"]["sample_ok"]["ok"])
        self.assertEqual(data["verdict"], "no-go")
        self.assertFalse(data["liveEnabled"])

    def test_amm_phase_uses_ten_bps_floor(self):
        """GRADMOCK-style AMM floor is half of default spread (10), not the curve 62.5."""
        self.assertAlmostEqual(protocol_fee_bps_for_phase("amm"), AMM_IMPACT_FEE_FLOOR_BPS)
        self.assertAlmostEqual(AMM_IMPACT_FEE_FLOOR_BPS, 10.0)
        recipe = _load("go_30.json")
        trades = _expand_trades(recipe)
        for row in trades:
            row["entry_estimated_impact_bps"] = 75.0
            row["phase"] = "amm"
        data = aggregate_executability(trades, eval_counts=recipe["eval_counts"])
        self.assertAlmostEqual(data["protocol_fee_bps"], 10.0)
        self.assertAlmostEqual(data["median_entry_impact_gross_bps"], 75.0)
        self.assertAlmostEqual(data["median_entry_impact_net_bps"], 65.0)
        self.assertLessEqual(data["max_entry_impact_bps"], IMPACT_HARD_MAX_BPS)
        self.assertFalse(data["gates"]["median_entry_impact"]["ok"])
        self.assertEqual(data["verdict"], "no-go")
        self.assertFalse(data["liveEnabled"])

    def test_decision_log_row_exposes_gross_fee_net(self):
        row = make_row(
            ts=1,
            strategy_id="pump-paper-v1",
            symbol="PUMPDEMO/SOL",
            stage="paper_submit",
            outcome="fill",
            signal_side="buy",
            impact_bps_est=76.0,
            phase="curve",
        )
        self.assertAlmostEqual(row.impact_gross_bps, 76.0)
        self.assertAlmostEqual(row.protocol_fee_bps, 62.5)
        self.assertAlmostEqual(row.impact_net_bps, 13.5)
        self.assertEqual(row.phase, "curve")
        amm = make_row(
            ts=2,
            strategy_id="pump-paper-v1",
            symbol="GRADMOCK/SOL",
            stage="paper_submit",
            outcome="fill",
            signal_side="buy",
            impact_bps_est=20.0,
            phase="amm",
        )
        self.assertAlmostEqual(amm.protocol_fee_bps, 10.0)
        self.assertAlmostEqual(amm.impact_net_bps, 10.0)

    def test_hard_max_impact_breach(self):
        recipe = _load("go_30.json")
        trades = _expand_trades(recipe)
        trades[0]["entry_estimated_impact_bps"] = 90.0
        data = aggregate_executability(trades, eval_counts=recipe["eval_counts"])
        self.assertEqual(data["verdict"], "no-go")
        self.assertFalse(data["gates"]["median_entry_impact"]["ok"])
        self.assertGreater(data["max_entry_impact_bps"], IMPACT_HARD_MAX_BPS)

    def test_reject_rate_from_decision_log_fixture(self):
        raw = _load("decision_log_mix.json")
        data = aggregate_executability([], decision_log=raw["items"])
        self.assertEqual(data["n_closed"], 0)
        self.assertEqual(data["verdict"], "no-go")
        self.assertEqual(data["lamp"], "gray")
        by = data["reject_rate"]
        self.assertEqual(by["progress"]["count"], 2)
        self.assertEqual(by["impact"]["count"], 2)  # impact + DEPTH_THIN
        self.assertEqual(by["risk"]["count"], 1)
        self.assertTrue(data["gates"]["reject_rate"]["ok"])
        self.assertFalse(data["liveEnabled"])
        self.assertIn("progress", by)
        self.assertNotIn("progress_band", by)
        self.assertIn("impact_error", data)

    def test_reject_rate_from_legacy_decision_fixture(self):
        raw = _load("decisions_mix.json")
        data = aggregate_executability([], decisions=raw["decisions"])
        self.assertEqual(data["reject_rate"]["progress"]["count"], 2)
        self.assertEqual(data["reject_rate"]["impact"]["count"], 1)
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
        self.assertEqual(MIN_CLOSED_TRADES, 30)
        self.assertEqual(IMPACT_MEDIAN_MAX_BPS, 60.0)
        self.assertEqual(IMPACT_HARD_MAX_BPS, 80.0)


def _replay(trade: dict, bps: float, source: str = "next_trade") -> dict:
    return {
        "ts": trade["entry_ts"],
        "symbol": trade["symbol"],
        "outcome": "fill",
        "signal_side": "buy",
        "shadow_source": source,
        "shadow_slippage_bps": bps,
    }


class ShadowCoverageMergeTests(unittest.TestCase):
    """G5 coverage uses one shadow per close. P50 stays ≤40; P90 does not gate."""

    def _trades(self, n: int, shadow: float = 15.0) -> tuple[list[dict], dict]:
        recipe = _load("go_30.json")
        recipe = dict(recipe)
        recipe["n"] = n
        recipe["entry_shadow_slippage_bps"] = shadow
        return _expand_trades(recipe), recipe["eval_counts"]

    def test_coverage_fail_message_does_not_cite_p50(self):
        """n_closed=60, P50≤40, but only 10 trades have a shadow → coverage, not P50."""
        trades, evals = self._trades(60, shadow=10.0)
        for row in trades[10:]:
            row["entry_shadow_slippage_bps"] = None
        # Same 10 closes also have a replay. Must not become n=20.
        log = [_replay(row, 22.3, "next_trade") for row in trades[:10]]
        data = aggregate_executability(trades, decision_log=log, eval_counts=evals)
        gate = data["gates"]["shadow_slippage"]
        self.assertEqual(data["n_closed"], 60)
        self.assertEqual(gate["n"], 10)
        self.assertEqual(data["shadow_slippage"]["n"], 10)
        self.assertEqual(gate["min_n"], 30)
        self.assertEqual(data["shadow_slippage"]["min_n"], 30)
        self.assertFalse(gate["coverage_ok"])
        self.assertFalse(data["shadow_slippage"]["coverage_ok"])
        self.assertAlmostEqual(gate["p50_bps"], 22.3)
        self.assertLessEqual(gate["p50_bps"], SHADOW_SLIPPAGE_X_BPS)
        self.assertFalse(gate["ok"])
        self.assertEqual(data["nogo_reason"], "shadow coverage 10/30")
        self.assertNotIn("P50", data["nogo_reason"])
        self.assertEqual(data["verdict"], "no-go")
        self.assertEqual(data["lamp"], "gray")
        self.assertFalse(data["liveEnabled"])
        self.assertFalse(LIVE_ENABLED)

    def test_journal_fill_quote_fills_replay_gap_and_passes(self):
        """Ten next_trade|next_open rows plus journal fill-quote on the rest reach n=60."""
        trades, evals = self._trades(60, shadow=20.0)
        for row in trades[-8:]:
            row["entry_shadow_slippage_bps"] = 90.0
        log = [_replay(row, 22.3, "next_trade") for row in trades[:5]]
        log += [_replay(row, 18.0, "next_open") for row in trades[5:10]]
        data = aggregate_executability(trades, decision_log=log, eval_counts=evals)
        gate = data["gates"]["shadow_slippage"]
        self.assertEqual(gate["n"], 60)
        self.assertEqual(gate["min_n"], MIN_CLOSED_TRADES)
        self.assertTrue(gate["coverage_ok"])
        self.assertLessEqual(gate["p50_bps"], SHADOW_SLIPPAGE_X_BPS)
        self.assertGreater(gate["p90_bps"], SHADOW_SLIPPAGE_X_BPS)
        self.assertTrue(gate["ok"])
        self.assertEqual(data["verdict"], "go")
        self.assertEqual(data["lamp"], "green")
        self.assertNotIn("shadow coverage", data["nogo_reason"])
        self.assertFalse(data["liveEnabled"])
        self.assertFalse(PumpPaperParams().auto_paper_orders)

    def test_replay_and_journal_are_not_double_counted(self):
        """DecisionLog wins for a trade; journal and a same-ts next_open do not add rows."""
        trades, evals = self._trades(30, shadow=10.0)
        log: list[dict] = []
        for row in trades:
            log.append(_replay(row, 25.0, "next_trade"))
            log.append(_replay(row, 5.0, "next_open"))
            log.append(_replay(row, 99.0, "fill_quote"))
        data = aggregate_executability(trades, decision_log=log, eval_counts=evals)
        gate = data["gates"]["shadow_slippage"]
        self.assertEqual(data["n_closed"], 30)
        self.assertEqual(gate["n"], 30)
        self.assertAlmostEqual(gate["p50_bps"], 25.0)
        self.assertTrue(gate["coverage_ok"])
        self.assertTrue(gate["ok"])
        self.assertEqual(data["verdict"], "go")
        self.assertFalse(data["liveEnabled"])


class AnnotateFillTests(unittest.TestCase):
    def test_shadow_from_mid(self):
        ctx = StrategyContext(symbol="PUMPDEMO/SOL", ts=1, tick=TickCtx(mid=1.0))
        intent = OrderIntent(side="buy", qty_or_notional=0.1)
        fill = Fill(ts=1, price=1.002, qty=0.1)
        out = annotate_fill_executability(fill, ctx, intent)
        self.assertAlmostEqual(out.quote_price, 1.0)
        self.assertAlmostEqual(out.shadow_slippage_bps, 20.0)
        self.assertIsNotNone(out.estimated_impact_bps)
        self.assertAlmostEqual(out.estimated_impact_gross_bps, out.estimated_impact_bps)
        self.assertIsNotNone(out.protocol_fee_bps)
        self.assertIsNotNone(out.estimated_impact_net_bps)
        self.assertGreaterEqual(out.estimated_impact_net_bps, 0.0)
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
        self.assertAlmostEqual(rt.entry_estimated_impact_gross_bps, 41.0)
        self.assertAlmostEqual(rt.entry_protocol_fee_bps, CURVE_IMPACT_FEE_FLOOR_BPS)
        self.assertAlmostEqual(rt.entry_estimated_impact_net_bps, 0.0)
        self.assertAlmostEqual(rt.entry_shadow_slippage_bps, 10.0)
        dumped = rt.as_dict()
        self.assertIn("entry_estimated_impact_bps", dumped)
        self.assertIn("entry_estimated_impact_gross_bps", dumped)
        self.assertIn("entry_estimated_impact_net_bps", dumped)
        self.assertIn("entry_protocol_fee_bps", dumped)


class ExpectancyAndImpactCoverageTests(unittest.TestCase):
    def test_net_median_under_60_ok_when_closed_n_is_sufficient(self):
        """Gross 79.5 − curve fee 62.5 = net 17. Thirty closes keep n=30.

        A DecisionLog that only still has 3 entry fills stays a backfill source.
        The impact gate uses the net median and stays ok. Live stays off.
        """
        recipe = _load("go_30.json")
        trades = _expand_trades(recipe)
        for row in trades:
            row["entry_estimated_impact_bps"] = 79.5
        short_log = [
            {
                "ts": i,
                "symbol": "PUMPDEMO/SOL",
                "stage": "paper_submit",
                "outcome": "fill",
                "signal_side": "buy",
                "impact_gross_bps": 79.5,
                "protocol_fee_bps": 62.5,
                "impact_net_bps": 17.0,
            }
            for i in range(3)
        ]
        data = aggregate_executability(
            trades, decision_log=short_log, eval_counts=recipe["eval_counts"]
        )
        gate = data["gates"]["median_entry_impact"]
        self.assertEqual(data["n_closed"], 30)
        self.assertEqual(gate["n"], 30)
        self.assertAlmostEqual(data["median_entry_impact_gross_bps"], 79.5)
        self.assertAlmostEqual(data["median_entry_impact_net_bps"], 17.0)
        self.assertLess(data["median_entry_impact_net_bps"], IMPACT_MEDIAN_MAX_BPS)
        self.assertLessEqual(data["max_entry_impact_gross_bps"], IMPACT_HARD_MAX_BPS)
        self.assertTrue(gate["ok"])
        self.assertEqual(gate["basis"], "net_of_protocol_fee")
        self.assertTrue(data["sample_ok"])
        self.assertTrue(data["gates"]["expectancy"]["ok"])
        self.assertEqual(data["verdict"], "go")
        self.assertFalse(data["liveEnabled"])
        self.assertFalse(LIVE_ENABLED)

    def test_negative_expectancy_blocks_when_impact_net_passes(self):
        recipe = _load("go_30.json")
        trades = _expand_trades(recipe)
        for row in trades:
            row["pnl"] = -0.003
            row["entry_estimated_impact_bps"] = 79.5
        data = aggregate_executability(trades, eval_counts=recipe["eval_counts"])
        self.assertTrue(data["sample_ok"])
        self.assertAlmostEqual(data["expectancy"], -0.003)
        self.assertFalse(data["gates"]["expectancy"]["ok"])
        self.assertTrue(data["gates"]["median_entry_impact"]["ok"])
        self.assertAlmostEqual(data["median_entry_impact_net_bps"], 17.0)
        self.assertEqual(data["gates"]["median_entry_impact"]["n"], 30)
        self.assertEqual(data["verdict"], "no-go")
        self.assertFalse(data["liveEnabled"])

    def test_sparse_impacts_do_not_fail_the_60_or_80_gate(self):
        """A short impact sample is missing evidence, not a net>=60 or gross>80 fail."""
        recipe = _load("go_30.json")
        trades = _expand_trades(recipe)
        for row in trades:
            row.pop("entry_estimated_impact_bps", None)
        log = [
            {
                "ts": 1_000 + i,
                "symbol": "PUMPDEMO/SOL",
                "outcome": "fill",
                "signal_side": "buy",
                "impact_gross_bps": 79.5,
                "protocol_fee_bps": 62.5,
                "impact_net_bps": 17.0,
            }
            for i in range(3)
        ]
        data = aggregate_executability(trades, decision_log=log, eval_counts=recipe["eval_counts"])
        self.assertEqual(data["n_closed"], 30)
        self.assertEqual(data["gates"]["median_entry_impact"]["n"], 3)
        self.assertAlmostEqual(data["median_entry_impact_net_bps"], 17.0)
        gate = data["gates"]["median_entry_impact"]
        self.assertTrue(gate["ok"])
        self.assertFalse(gate["net_fail"])
        self.assertFalse(gate["gross_fail"])
        self.assertFalse(gate["coverage_ok"])
        self.assertEqual(gate["fails_only_on"], "net_median>=60 OR any_gross>80")
        self.assertEqual(data["verdict"], "no-go")
        self.assertFalse(data["liveEnabled"])

    def test_impact_gate_fails_only_on_net_60_or_gross_over_80(self):
        recipe = _load("go_30.json")
        trades = _expand_trades(recipe)

        def _run(gross: float) -> dict:
            rows = [dict(row) for row in trades]
            for row in rows:
                row["entry_estimated_impact_bps"] = gross
            return aggregate_executability(rows, eval_counts=recipe["eval_counts"])

        at_hard = _run(80.0)
        self.assertAlmostEqual(at_hard["median_entry_impact_gross_bps"], 80.0)
        self.assertAlmostEqual(at_hard["median_entry_impact_net_bps"], 17.5)
        self.assertTrue(at_hard["gates"]["median_entry_impact"]["ok"])
        self.assertFalse(at_hard["gates"]["median_entry_impact"]["gross_fail"])
        self.assertFalse(at_hard["gates"]["median_entry_impact"]["net_fail"])

        over_hard = _run(80.1)
        self.assertFalse(over_hard["gates"]["median_entry_impact"]["ok"])
        self.assertTrue(over_hard["gates"]["median_entry_impact"]["gross_fail"])
        self.assertLess(over_hard["median_entry_impact_net_bps"], IMPACT_MEDIAN_MAX_BPS)

        net_60 = _expand_trades(recipe)
        for row in net_60:
            row["entry_estimated_impact_bps"] = 80.0
            row["entry_estimated_impact_gross_bps"] = 80.0
            row["entry_protocol_fee_bps"] = 20.0
            row["entry_estimated_impact_net_bps"] = 60.0
        net_hit = aggregate_executability(net_60, eval_counts=recipe["eval_counts"])
        self.assertAlmostEqual(net_hit["median_entry_impact_net_bps"], 60.0)
        self.assertAlmostEqual(net_hit["max_entry_impact_gross_bps"], 80.0)
        self.assertFalse(net_hit["gates"]["median_entry_impact"]["ok"])
        self.assertTrue(net_hit["gates"]["median_entry_impact"]["net_fail"])
        self.assertFalse(net_hit["gates"]["median_entry_impact"]["gross_fail"])
        self.assertEqual(net_hit["verdict"], "no-go")
        self.assertFalse(net_hit["liveEnabled"])
        self.assertEqual(IMPACT_MEDIAN_MAX_BPS, 60.0)
        self.assertEqual(IMPACT_HARD_MAX_BPS, 80.0)

    def test_fill_backfill_makes_impact_n_match_n_closed(self):
        recipe = _load("go_30.json")
        trades = _expand_trades(recipe)
        fills = []
        for row in trades:
            row.pop("entry_estimated_impact_bps", None)
            fills.append(
                {
                    "symbol": row["symbol"],
                    "ts": row["entry_ts"],
                    "qty": 1.0,
                    "estimated_impact_bps": 79.5,
                    "estimated_impact_gross_bps": 79.5,
                    "protocol_fee_bps": 62.5,
                    "estimated_impact_net_bps": 17.0,
                }
            )
        data = aggregate_executability(
            trades, fills=fills, eval_counts=recipe["eval_counts"]
        )
        self.assertEqual(data["n_closed"], MIN_CLOSED_TRADES)
        self.assertEqual(data["gates"]["median_entry_impact"]["n"], data["n_closed"])
        self.assertAlmostEqual(data["median_entry_impact_net_bps"], 17.0)
        self.assertTrue(data["gates"]["median_entry_impact"]["ok"])
        self.assertFalse(data["liveEnabled"])

    def test_journal_close_writes_gross_fee_net_for_every_trade(self):
        led = PaperTradeJournal()
        for i in range(4):
            led.record_fill(
                "A/SOL",
                Fill(
                    ts=10 + i * 2,
                    price=1.0,
                    qty=1.0,
                    estimated_impact_bps=79.5,
                    tag="paper:pump-paper-v1",
                ),
            )
            led.record_fill(
                "A/SOL",
                Fill(
                    ts=11 + i * 2,
                    price=1.02,
                    qty=-1.0,
                    tag="paper:pump-paper-v1:flat",
                ),
            )
        self.assertEqual(len(led.closed), 4)
        for rt in led.closed:
            self.assertAlmostEqual(rt.entry_estimated_impact_gross_bps, 79.5)
            self.assertAlmostEqual(rt.entry_protocol_fee_bps, 62.5)
            self.assertAlmostEqual(rt.entry_estimated_impact_net_bps, 17.0)
        data = aggregate_executability([t.as_dict() for t in led.closed])
        self.assertEqual(data["gates"]["median_entry_impact"]["n"], data["n_closed"])
        self.assertEqual(data["n_closed"], 4)
        self.assertFalse(data["liveEnabled"])


class EngineEvalCountTests(unittest.TestCase):
    def test_record_entry_eval_buckets(self):
        from app.models.contracts import SignalOut

        eng = PumpPaperEngine(PumpPaperParams(auto_paper_orders=False))
        eng.record_entry_eval(SignalOut(side="flat", reason="progress_band"))
        eng.record_entry_eval(SignalOut(side="flat", reason="impact", tags=["SLIPPAGE_CAP"]))
        eng.record_entry_eval(SignalOut(side="long", reason="pump_paper_v1_entry"))
        snap = eng.eval_snapshot()
        self.assertEqual(snap["total"], 3)
        self.assertEqual(snap["by_bucket"]["progress"], 1)
        self.assertEqual(snap["by_bucket"]["impact"], 1)
        self.assertEqual(snap["by_bucket"]["attempt"], 1)


class NoChainSendTests(unittest.TestCase):
    def test_modules_have_no_send(self):
        files = [
            ROOT / "apps" / "api" / "app" / "paper" / "executability.py",
            ROOT / "apps" / "api" / "app" / "paper" / "decision_log.py",
            ROOT / "apps" / "api" / "app" / "routes" / "stats.py",
        ]
        banned = ("sendtransaction", "sniper", "private_key", "hftbacktest", "nautilus", "backtrader", "freqtrade")
        for path in files:
            text = path.read_text(encoding="utf-8").lower()
            for needle in banned:
                self.assertNotIn(needle, text, f"{path.name} must not mention {needle}")


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
        reset_decision_log()

    def test_empty_endpoint_is_nogo_live_false(self):
        r = self.client.get("/api/v1/stats/executability")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["ok"])
        data = body["data"]
        self.assertEqual(data["verdict"], "no-go")
        self.assertEqual(data["lamp"], "gray")
        self.assertIn("平仓样本", data["nogo_reason"])
        self.assertFalse(data["liveEnabled"])
        self.assertTrue(data["liveDisabled"])
        self.assertFalse(data["gates"]["live"]["ok"])
        self.assertIn("liveEnabled", data["gates"]["live"]["reason"])
        self.assertIn("LiveLimits", data["gates"]["live"]["reason"])
        self.assertEqual(data["live_limits"]["max_notional_sol"], 1.0)
        self.assertEqual(data["hard_max_impact_bps"], 80.0)
        self.assertAlmostEqual(data["protocol_fee_bps_curve"], 62.5)
        self.assertAlmostEqual(data["protocol_fee_bps_amm"], 10.0)
        self.assertEqual(data["gates"]["median_entry_impact"]["basis"], "net_of_protocol_fee")
        self.assertEqual(data["n_closed"], 0)
        self.assertIn("progress", data["reject_rate"])
        self.assertIn("impact", data["reject_rate"])
        self.assertIn("risk", data["reject_rate"])
        self.assertFalse(data["live_checks"]["secondary_confirm"])
        self.assertFalse(data["live_checks"]["keypair_mounted"])
        self.assertIn("impact_error", data)
        blob = json.dumps(data).lower()
        self.assertNotIn("secret", blob)
        self.assertNotIn("private_key", blob)
        self.assertNotIn("mnemonic", blob)

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
        self.assertIsNotNone(f.get("estimated_impact_gross_bps"))
        self.assertIsNotNone(f.get("protocol_fee_bps"))
        self.assertIsNotNone(f.get("estimated_impact_net_bps"))
        self.assertIsNotNone(f.get("shadow_slippage_bps"))
        self.client.post(
            "/api/v1/pipeline/decide-and-fill",
            json={"symbol": "PUMPDEMO/SOL", "side": "sell", "notional": 0.05},
        )
        r = self.client.get("/api/v1/stats/executability")
        data = r.json()["data"]
        self.assertGreaterEqual(data["n_closed"], 1)
        self.assertEqual(data["gates"]["median_entry_impact"]["n"], data["n_closed"])
        self.assertFalse(data["liveEnabled"])
        stats = self.client.get("/api/v1/strategy/pump-paper-v1/stats")
        self.assertEqual(stats.status_code, 200)
        journal = stats.json()["data"]["journal"]
        self.assertGreaterEqual(len(journal), 1)
        closed = journal[-1]
        for key in (
            "entry_estimated_impact_gross_bps",
            "entry_protocol_fee_bps",
            "entry_estimated_impact_net_bps",
        ):
            self.assertIn(key, closed)
            self.assertIsNotNone(closed[key])
        self.assertEqual(data["verdict"], "no-go")  # sample < 30
        self.assertEqual(data["lamp"], "gray")
        log = self.client.get("/api/v1/strategy/pump-paper-v1/decision-log")
        self.assertTrue(log.json()["ok"])
        items = log.json()["data"]["items"]
        self.assertGreaterEqual(len(items), 1)
        stages = {row["stage"] for row in items}
        self.assertTrue(stages & {"pre_order", "paper_submit"})
        self.assertFalse(log.json()["data"]["liveEnabled"])
        for row in items:
            self.assertIn(row["reject_bucket"], ("progress", "impact", "risk", "none"))
        fills_log = [row for row in items if row["outcome"] in ("fill", "partial")]
        self.assertGreaterEqual(len(fills_log), 1)
        self.assertIn("estimated_impact_bps", fills_log[0])
        self.assertIn("impact_gross_bps", fills_log[0])
        self.assertIn("protocol_fee_bps", fills_log[0])
        self.assertIn("impact_net_bps", fills_log[0])
        self.assertAlmostEqual(fills_log[0]["protocol_fee_bps"], 62.5)
        self.assertAlmostEqual(
            fills_log[0]["impact_net_bps"],
            max(0.0, float(fills_log[0]["impact_gross_bps"]) - 62.5),
        )
        self.assertIn("median_entry_impact_net_bps", data)
        self.assertIn("median_entry_impact_gross_bps", data)
        self.assertIn("decision_px", fills_log[0])
        self.assertIn("paper_fill_px", fills_log[0])
        self.assertIn("shadow_fill_px", fills_log[0])
        self.assertIn("impact_error_bps", fills_log[0])

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
