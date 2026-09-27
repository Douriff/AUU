"""Paper postmortem + ExecReport (v0.1) — fixtures + scenario gate."""
from __future__ import annotations

import json
import os
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app
from app.paper.decision_log import reset_decision_log
from app.paper.ledger import reset_paper_ledger
from app.paper.postmortem import (
    GO_WINDOW_LABEL,
    ScenarioProgressForbidden,
    aggregate_postmortem,
    exit_reason_of,
    parse_scenario,
    params_fingerprint,
)
from app.risk.gate import reset_risk_gate
from app.strategies.pump_paper_v1 import PumpPaperParams, reset_engine

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "postmortem"


def _load_list(name: str) -> list[dict]:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def _expand(
    template: dict,
    n: int,
    *,
    tag: str | None = None,
    impact: float | None = None,
    pnl: float | None = None,
) -> list[dict]:
    rows: list[dict] = []
    for i in range(n):
        row = dict(template)
        row["id"] = f"{template.get('id', 'rt')}-{i}"
        row["entry_ts"] = int(template.get("entry_ts") or 1000) + i * 10
        row["exit_ts"] = int(template.get("exit_ts") or 2000) + i * 10
        if tag is not None:
            row["tags"] = [tag, "pump-paper-v1"]
        if impact is not None:
            row["entry_estimated_impact_bps"] = float(impact)
            row["entry_estimated_impact_net_bps"] = float(impact)
        if pnl is not None:
            row["pnl"] = float(pnl)
        rows.append(row)
    return rows


class ExitReasonTests(unittest.TestCase):
    def test_orphan_beats_max_hold(self):
        self.assertEqual(exit_reason_of({"tags": ["MAX_HOLD", "ORPHAN_EXIT"]}), "orphan")

    def test_tag_map(self):
        self.assertEqual(exit_reason_of({"tags": ["TAKE_PROFIT"]}), "take_profit")
        self.assertEqual(exit_reason_of({"tags": ["SELL_PRESSURE"]}), "sell_pressure")


class FixtureAggregateTests(unittest.TestCase):
    def test_mh_dominant(self):
        base = _load_list("mh_dominant.json")[0]
        trades = _expand(base, 20, tag="MAX_HOLD", pnl=0.01)
        trades += _expand(base, 10, tag="TAKE_PROFIT", pnl=0.06)
        report = aggregate_postmortem(trades, window_label="session", params=PumpPaperParams().model_dump())
        mh = next(b for b in report["by_exit_reason"] if b["reason"] == "max_hold")
        self.assertGreaterEqual(mh["pct"], 0.40)
        ids = {f["id"] for f in report["findings"]}
        self.assertIn("MH_DOMINANT", ids)
        self.assertEqual(report["go_window_label"], GO_WINDOW_LABEL)
        self.assertIsNone(report["exec"]["live_hint"])
        self.assertFalse(report["liveEnabled"])

    def test_tp_dominant(self):
        base = _load_list("tp_dominant.json")[0]
        trades = _expand(base, 18, tag="TAKE_PROFIT", pnl=0.06)
        trades += _expand(base, 12, tag="STOP_LOSS", pnl=-0.03)
        report = aggregate_postmortem(trades, window_label="session", params=PumpPaperParams().model_dump())
        tp = next(b for b in report["by_exit_reason"] if b["reason"] == "take_profit")
        self.assertGreaterEqual(tp["pct"], 0.30)
        ids = {f["id"] for f in report["findings"]}
        self.assertIn("TP_HEALTHY", ids)

    def test_q4_drag(self):
        low, high = _load_list("q4_drag.json")
        trades = _expand(low, 24, tag="TAKE_PROFIT", impact=12.0, pnl=0.05)
        trades += _expand(high, 8, tag="STOP_LOSS", impact=68.0, pnl=-0.10)
        report = aggregate_postmortem(trades, window_label="session", params=PumpPaperParams().model_dump())
        q4 = next(b for b in report["by_impact_quartile"] if b["q"] == "Q4")
        self.assertGreater(q4["count"], 0)
        self.assertIsNotNone(q4["expectancy"])
        self.assertLess(q4["expectancy"], 0)
        ids = {f["id"] for f in report["findings"]}
        self.assertIn("IMPACT_Q4_DRAG", ids)

    def test_exec_report_nested_and_go_blocked(self):
        base = _load_list("mh_dominant.json")[0]
        trades = _expand(base, 5, tag="MAX_HOLD", pnl=-0.02)
        report = aggregate_postmortem(trades, window_label="session", params=PumpPaperParams().model_dump())
        exec_ = report["exec"]
        self.assertEqual(exec_["gates"]["n_closed"], 5)
        self.assertFalse(exec_["gates"]["sample_ok"])
        self.assertFalse(exec_["gates"]["overall_go"])
        self.assertFalse(exec_["live_language_allowed"])
        self.assertIsNone(exec_["live_hint"])
        ids = {f["id"] for f in report["findings"]}
        self.assertIn("GO_BLOCKED", ids)
        self.assertIn("SAMPLE_THIN", ids)
        for f in report["findings"]:
            blob = f"{f['title']}{f['detail']}".lower()
            self.assertNotIn("liveenabled", blob)
            self.assertNotIn("开启实盘", blob)
            self.assertNotIn("enable live", blob)

    def test_scenario_default_off(self):
        report = aggregate_postmortem([], window_label="session", params=PumpPaperParams().model_dump())
        self.assertEqual(report["scenario"]["tag"], "off")
        self.assertFalse(report["scenario"]["enabled"])
        self.assertIn("scenario", report["debug"])

    def test_setup_seed_notes_only(self):
        report = aggregate_postmortem(
            [],
            window_label="session",
            params=PumpPaperParams().model_dump(),
            setup_seed_tags=["sniper_seed"],
        )
        self.assertTrue(any("setup_seed_tags" in n for n in report["notes"]))
        self.assertEqual(report["debug"]["setup_seed_tags"], ["sniper_seed"])


class ScenarioParseTests(unittest.TestCase):
    def test_momentum_delta_ok(self):
        state = parse_scenario(
            tag="momentum_delta",
            min_trade_count_1m=12,
            min_buy_sell_ratio_1m=2.0,
            parent_fingerprint="abc",
        )
        self.assertEqual(state["tag"], "momentum_delta")
        self.assertTrue(state["enabled"])
        self.assertEqual(state["delta"]["min_trade_count_1m"], 12)
        self.assertEqual(state["debug"]["scenario"]["parent_fingerprint"], "abc")

    def test_progress_forbidden(self):
        with self.assertRaises(ScenarioProgressForbidden) as ctx:
            parse_scenario(tag="momentum_delta", min_trade_count_1m=12, progress_bps_min=1500)
        self.assertEqual(ctx.exception.code, "SCENARIO_PROGRESS_FORBIDDEN")
        self.assertIn("progress_bps_min", ctx.exception.touched)

    def test_progress_forbidden_even_when_off(self):
        with self.assertRaises(ScenarioProgressForbidden):
            parse_scenario(tag="off", progress_bps_max=6000)


class ParamsFingerprintTests(unittest.TestCase):
    def test_stable(self):
        p = PumpPaperParams().model_dump()
        self.assertEqual(params_fingerprint(p), params_fingerprint(p))
        self.assertEqual(len(params_fingerprint(p)), 16)


class HttpRouteTests(unittest.TestCase):
    def setUp(self):
        os.environ["AUU_PROVIDER"] = "mock"
        reset_paper_ledger(wipe_store=True)
        reset_decision_log()
        reset_risk_gate()
        reset_engine()
        self.client = TestClient(app)

    def tearDown(self):
        reset_paper_ledger(wipe_store=True)
        reset_decision_log()
        reset_engine()

    def test_postmortem_primary_and_alias(self):
        r = self.client.get("/api/v1/strategy/pump-paper-v1/postmortem")
        self.assertEqual(r.status_code, 200)
        data = r.json()["data"]
        self.assertTrue(r.json()["ok"])
        self.assertIn("exec", data)
        self.assertIn("findings", data)
        self.assertIn("scenario", data)
        self.assertEqual(data["go_window_label"], "round8b")
        self.assertIsNone(data["exec"]["live_hint"])
        self.assertFalse(data["liveEnabled"])

        alias = self.client.get("/api/v1/stats/postmortem")
        self.assertEqual(alias.status_code, 200)
        self.assertTrue(alias.json()["ok"])

        er = self.client.get("/api/v1/stats/exec-report")
        self.assertEqual(er.status_code, 200)
        er_data = er.json()["data"]
        self.assertIn("overall_go", er_data["gates"])
        self.assertIsNone(er_data["live_hint"])
        self.assertEqual(er_data["live_language_allowed"], er_data["gates"]["overall_go"])

    def test_scenario_progress_forbidden_http(self):
        r = self.client.get(
            "/api/v1/strategy/pump-paper-v1/postmortem",
            params={"scenario": "momentum_delta", "min_trade_count_1m": 12, "progress_bps_min": 1500},
        )
        self.assertEqual(r.status_code, 400)
        err = r.json()["error"]
        self.assertEqual(err["code"], "SCENARIO_PROGRESS_FORBIDDEN")
        self.assertIn("progress_bps_min", err.get("forbidden_touched") or [])

    def test_defaults_untouched_round8b(self):
        p = self.client.get("/api/v1/strategy/pump-paper-v1").json()["data"]["params"]
        self.assertEqual(p["progress_bps_min"], 1500)
        self.assertEqual(p["progress_bps_max"], 6000)
        self.assertEqual(p["take_profit_pct"], 0.06)
        self.assertEqual(p["stop_loss_pct"], 0.05)
        self.assertEqual(p["max_hold_sec"], 120)
        self.assertEqual(p["sell_pressure_sec"], 5.0)
        self.assertEqual(p["sell_pressure_ratio"], 1.0)
        self.assertEqual(p["min_trade_count_1m"], 10)
        self.assertEqual(p["min_buy_sell_ratio_1m"], 2.5)
        self.assertFalse(p["auto_paper_orders"])


if __name__ == "__main__":
    unittest.main()
