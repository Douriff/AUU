"""Shadow parameter comparison — virtual, paper-only, isolated from Go stats."""
from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from app.main import app
from app.models.contracts import PumpfunPaperSnapshot, TraderWatchlistItem
from app.paper.decision_log import get_decision_log, reset_decision_log
from app.legacy.pump.paper.executability import build_executability
from app.paper.ledger import get_paper_journal, reset_paper_ledger
from app.paper.events import build_events, reset_events
from app.legacy.pump.paper.shadow_compare import (
    ShadowProgressForbidden,
    apply_shadow_config,
    build_shadow_compare,
    delta_from_habit,
    observe_candidate,
    progress_keys,
    reload_shadow_from_disk,
    reset_shadow_compare,
    shadow_closed,
    shadow_decisions,
)
from app.risk.gate import reset_risk_gate
from app.legacy.pump.strategies.pump_paper_v1 import (
    PumpPaperEngine,
    PumpPaperParams,
    TapeWindow,
    get_engine,
    reset_engine,
)
from app.legacy.pump.traders.habits import build_profile, habit_profile
from app.legacy.pump.traders.snapshot import mock_snapshot_for
from app.legacy.pump.traders.store import list_watches

NOW = 1_700_000_000_000


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
        price_sol=30_000_000_000 / 1_073_000_000_000_000,
        creator_fee_bps=0,
        updated_ts=1,
        synthetic=False,
    )
    base.update(kwargs)
    return PumpfunPaperSnapshot(**base)


def _hot() -> TapeWindow:
    return TapeWindow(buy_notional_1m=4.0, sell_notional_1m=1.0, trade_count_1m=12)


def _raise_price(snap: PumpfunPaperSnapshot, factor: float) -> PumpfunPaperSnapshot:
    vs = int(int(snap.virtual_sol_reserves) * factor)
    vt = int(snap.virtual_token_reserves)
    return snap.model_copy(
        update={
            "virtual_sol_reserves": str(vs),
            "price_sol": vs / vt,
        }
    )


def _item(watch_id: str, address: str, label: str) -> TraderWatchlistItem:
    return TraderWatchlistItem(
        watch_id=watch_id,
        address=address,
        label=label,
        enabled=True,
        source="rpc",
        added_ts=NOW,
    )


class ShadowEvalTests(unittest.TestCase):
    def setUp(self):
        # These tests drive shadow mechanics on the mock provider; label the
        # rows real so the compare counts them (mock rows are excluded).
        label = mock.patch("app.legacy.pump.paper.shadow_compare._market_source", return_value="real")
        label.start()
        self.addCleanup(label.stop)
        reset_shadow_compare()
        reset_paper_ledger(wipe_store=True)
        reset_decision_log()
        reset_risk_gate()
        reset_engine()

    def tearDown(self):
        reset_shadow_compare()
        reset_paper_ledger(wipe_store=True)
        reset_decision_log()
        reset_engine()

    def test_default_off_records_nothing(self):
        report = build_shadow_compare()
        self.assertFalse(report["enabled"])
        self.assertFalse(report["liveEnabled"])
        self.assertIn("never enable live", report["note"])
        observe_candidate(
            symbol="PUMPDEMO/SOL",
            snapshot=_snap(),
            tape=_hot(),
            now_ms=NOW,
            notional_sol=0.12,
            impact_entry_bps=20.0,
            main_params=PumpPaperParams(),
        )
        self.assertEqual(shadow_decisions(), [])
        self.assertEqual(shadow_closed(), [])
        self.assertEqual(get_paper_journal().closed, [])
        self.assertEqual(get_decision_log().rows, [])

    def test_enter_skip_and_hypothetical_tp(self):
        apply_shadow_config(
            {
                "enabled": True,
                "sets": [
                    {
                        "id": "loose",
                        "label": "loose",
                        "habit_tag": "flip",
                        "setup_seed_tags": ["seed-label"],
                        "min_trade_count_1m": 8,
                        "take_profit_pct": 0.06,
                        "max_hold_sec": 120,
                    },
                    {
                        "id": "strict",
                        "label": "strict",
                        "min_trade_count_1m": 50,
                        "max_hold_sec": 90,
                    },
                ],
            }
        )
        before = build_executability()
        snap = _snap()
        observe_candidate(
            symbol=snap.symbol,
            snapshot=snap,
            tape=_hot(),
            now_ms=NOW,
            notional_sol=0.12,
            impact_entry_bps=20.0,
            main_params=PumpPaperParams(),
        )
        actions = {(d["set_id"], d["action"], d["reason"]) for d in shadow_decisions()}
        self.assertIn(("loose", "enter", "pump_paper_v1_entry"), actions)
        self.assertIn(("strict", "skip", "momentum"), actions)
        self.assertEqual(shadow_closed(), [])

        observe_candidate(
            symbol=snap.symbol,
            snapshot=_raise_price(snap, 1.25),
            tape=_hot(),
            now_ms=NOW + 5_000,
            notional_sol=0.12,
            impact_entry_bps=20.0,
            main_params=PumpPaperParams(),
        )
        closed = shadow_closed("loose")
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0]["exit_reason"], "take_profit")
        self.assertGreater(closed[0]["pnl"], 0)
        self.assertGreater(closed[0]["net_bps"], 0)
        self.assertEqual(closed[0]["setup_seed_tags"], ["seed-label"])
        self.assertEqual(shadow_closed("strict"), [])
        self.assertIn("SHADOW", closed[0]["tags"])

        self.assertEqual(get_paper_journal().closed, [])
        self.assertEqual(get_decision_log().rows, [])
        self.assertEqual(get_engine().positions, {})
        self.assertEqual(get_engine().eval_snapshot()["total"], 0)
        after = build_executability()
        self.assertEqual(after["n_closed"], before["n_closed"])
        self.assertEqual(after["verdict"], before["verdict"])
        self.assertEqual(get_engine().params.model_dump(), PumpPaperParams().model_dump())

        report = build_shadow_compare()
        by_id = {s["id"]: s for s in report["sets"]}
        self.assertEqual(by_id["loose"]["n"], 1)
        self.assertFalse(by_id["loose"]["sample_ok"])
        self.assertEqual(by_id["strict"]["n"], 0)
        self.assertFalse(report["liveEnabled"])
        self.assertIn("never enable live", report["note"])
        tp = next(r for r in by_id["loose"]["exit_vs_main"] if r["reason"] == "take_profit")
        self.assertEqual(tp["shadow_count"], 1)
        self.assertEqual(tp["main_count"], 0)

    def test_sample_ok_at_thirty_and_hold_exit(self):
        apply_shadow_config(
            {
                "enabled": True,
                "sets": [{"id": "hold", "max_hold_sec": 1, "min_trade_count_1m": 8}],
            }
        )
        params = PumpPaperParams()
        for i in range(30):
            snap = _snap(symbol=f"M{i}/SOL", mint=f"mint{i}")
            observe_candidate(
                symbol=snap.symbol,
                snapshot=snap,
                tape=_hot(),
                now_ms=NOW + i * 10_000,
                notional_sol=0.12,
                impact_entry_bps=12.0,
                main_params=params,
            )
            observe_candidate(
                symbol=snap.symbol,
                snapshot=snap,
                tape=_hot(),
                now_ms=NOW + i * 10_000 + 2_000,
                notional_sol=0.12,
                impact_entry_bps=12.0,
                main_params=params,
            )
        rows = shadow_closed("hold")
        self.assertEqual(len(rows), 30)
        self.assertTrue(all(r["exit_reason"] == "max_hold" for r in rows))
        report = build_shadow_compare()
        col = report["sets"][0]
        self.assertEqual(col["n"], 30)
        self.assertTrue(col["sample_ok"])
        self.assertIsNotNone(col["win_rate"])
        self.assertIsNotNone(col["expectancy"])
        self.assertIsNotNone(col["median_net_bps"])
        self.assertFalse(report["main"]["sample_ok"])
        self.assertEqual(get_paper_journal().closed, [])

    def test_progress_keys_rejected(self):
        with self.assertRaises(ShadowProgressForbidden) as ctx:
            apply_shadow_config(
                {
                    "enabled": True,
                    "sets": [{"id": "bad", "max_hold_sec": 40, "progress_bps_min": 100}],
                }
            )
        self.assertEqual(ctx.exception.code, "SHADOW_PROGRESS_FORBIDDEN")
        self.assertIn("progress_bps_min", ctx.exception.touched)
        self.assertFalse(build_shadow_compare()["enabled"])
        self.assertEqual(progress_keys({"progress_bps_max": 9000}), ["progress_bps_max"])

    def test_config_and_counters_survive_restart(self):
        fd, name = tempfile.mkstemp(prefix="auu-shadow-", suffix=".json")
        os.close(fd)
        path = Path(name)
        os.environ["SHADOW_COMPARE_STORE"] = str(path)
        try:
            reset_shadow_compare()
            reset_events()
            apply_shadow_config(
                {
                    "enabled": True,
                    "sets": [
                        {
                            "id": "loose",
                            "label": "loose",
                            "min_trade_count_1m": 8,
                            "take_profit_pct": 0.06,
                            "max_hold_sec": 120,
                        }
                    ],
                }
            )
            snap = _snap()
            params = PumpPaperParams()
            observe_candidate(
                symbol=snap.symbol,
                snapshot=snap,
                tape=_hot(),
                now_ms=NOW,
                notional_sol=0.12,
                impact_entry_bps=20.0,
                main_params=params,
            )
            observe_candidate(
                symbol=snap.symbol,
                snapshot=_raise_price(snap, 1.25),
                tape=_hot(),
                now_ms=NOW + 5_000,
                notional_sol=0.12,
                impact_entry_bps=20.0,
                main_params=params,
            )
            self.assertEqual(build_shadow_compare()["sets"][0]["n"], 1)
            stored = json.loads(path.read_text(encoding="utf-8"))
            self.assertTrue(stored["enabled"])
            self.assertEqual(stored["sets"][0]["id"], "loose")
            self.assertEqual(stored["counters"]["n_closed"], 1)
            self.assertEqual(stored["counters"]["wins"], 1)
            self.assertGreater(stored["counters"]["pnl_sum"], 0)

            feed = build_events(limit=100)
            shadow_rows = [row for row in feed["events"] if row["type"] == "shadow"]
            self.assertTrue(any("影子" in row["message"] and "虚拟开仓" in row["message"] for row in shadow_rows))
            closed_rows = [row for row in shadow_rows if row.get("pnl") is not None]
            self.assertEqual(len(closed_rows), 1)
            self.assertGreater(closed_rows[0]["pnl"], 0)
            self.assertFalse(feed["liveEnabled"])

            reload_shadow_from_disk()
            report = build_shadow_compare()
            self.assertTrue(report["enabled"])
            self.assertFalse(report["liveEnabled"])
            self.assertEqual(report["sets"][0]["id"], "loose")
            self.assertEqual(report["sets"][0]["n"], 1)
            self.assertEqual(report["sets"][0]["params"]["max_hold_sec"], 120)
            self.assertEqual(get_engine().params.model_dump(), params.model_dump())

            reset_shadow_compare()
            self.assertFalse(path.exists())
            reload_shadow_from_disk()
            wiped = build_shadow_compare()
            self.assertFalse(wiped["enabled"])
            self.assertEqual(wiped["sets"], [])
        finally:
            os.environ.pop("SHADOW_COMPARE_STORE", None)
            reset_shadow_compare()
            reset_events()


class HabitDeriveTests(unittest.TestCase):
    def test_deltas_skip_progress_and_do_not_mutate_profile(self):
        main = PumpPaperParams()
        cases = {
            "sniper": "WatchSniper1111111111111111111111111111111",
            "mid_curve": "WatchMidCurve11111111111111111111111111111",
            "graduation_chase": "WatchGradChase1111111111111111111111111111",
            "flip": "WatchFlip111111111111111111111111111111111",
            "bag": "WatchBag1111111111111111111111111111111111",
        }
        for persona, address in cases.items():
            item = _item(f"watch-{persona}", address, persona)
            snap = mock_snapshot_for(item, now_ms=NOW, persona=persona)
            profile = build_profile(item, snap)
            before = profile.model_dump()
            delta = delta_from_habit(profile, snap, main)
            self.assertEqual(profile.model_dump(), before)
            self.assertFalse(any(k.startswith("progress") for k in delta))
            self.assertTrue(delta, persona)
            if persona == "flip":
                self.assertLess(delta["max_hold_sec"], main.max_hold_sec)
                self.assertLess(delta["take_profit_pct"], main.take_profit_pct)
            if persona == "bag":
                self.assertGreater(delta["max_hold_sec"], main.max_hold_sec)
            if persona == "sniper":
                self.assertLess(delta["min_trade_count_1m"], main.min_trade_count_1m)
            if persona == "graduation_chase":
                self.assertNotIn("progress_bps_max", delta)


class HttpShadowTests(unittest.TestCase):
    def setUp(self):
        os.environ["PUMP_PAPER_LOOP"] = "0"
        os.environ["PUMPFUN_DISCOVERY"] = "off"
        os.environ["TRADER_WATCH_READER"] = "mock"
        reset_shadow_compare()
        reset_paper_ledger(wipe_store=True)
        reset_decision_log()
        reset_risk_gate()
        reset_engine()
        self.client = TestClient(app)
        self.watches_before = [w.model_dump() for w in list_watches()]
        self.profiles_before = {}
        for w in list_watches():
            profile = habit_profile(w.watch_id, now_ms=NOW)
            if profile is not None:
                self.profiles_before[w.watch_id] = profile.model_dump()

    def tearDown(self):
        reset_shadow_compare()
        reset_paper_ledger(wipe_store=True)
        reset_decision_log()
        reset_engine()

    def test_get_default_off(self):
        r = self.client.get("/api/v1/strategy/pump-paper-v1/shadow-compare")
        self.assertEqual(r.status_code, 200)
        data = r.json()["data"]
        self.assertFalse(data["enabled"])
        self.assertFalse(data["liveEnabled"])
        self.assertEqual(data["main"]["id"], "main")
        self.assertEqual(data["sets"], [])
        self.assertIn("never enable live", data["note"])
        self.assertEqual(data["go_window_label"], "round8b")
        self.assertEqual(data["sample_min_n"], 30)
        self.assertFalse(data["main"]["sample_ok"])

    def test_progress_forbidden_400(self):
        r = self.client.put(
            "/api/v1/strategy/pump-paper-v1/shadow-compare",
            json={"enabled": True, "sets": [{"id": "x", "progress_bps_max": 9000, "max_hold_sec": 40}]},
        )
        self.assertEqual(r.status_code, 400)
        err = r.json()["error"]
        self.assertEqual(err["code"], "SHADOW_PROGRESS_FORBIDDEN")
        self.assertIn("progress_bps_max", err.get("forbidden_touched") or [])
        follow = self.client.get("/api/v1/strategy/pump-paper-v1/shadow-compare").json()["data"]
        self.assertFalse(follow["enabled"])
        params = self.client.get("/api/v1/strategy/pump-paper-v1").json()["data"]["params"]
        self.assertEqual(params["progress_bps_min"], 1500)
        self.assertEqual(params["progress_bps_max"], 6000)
        self.assertEqual(params["take_profit_pct"], 0.06)
        self.assertEqual(params["stop_loss_pct"], 0.05)
        self.assertEqual(params["max_hold_sec"], 120)
        self.assertFalse(params["auto_paper_orders"])

    def test_max_three_sets(self):
        sets = [{"id": f"s{i}", "max_hold_sec": 40 + i} for i in range(4)]
        r = self.client.put(
            "/api/v1/strategy/pump-paper-v1/shadow-compare",
            json={"enabled": True, "sets": sets},
        )
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["error"]["code"], "SHADOW_SET_LIMIT")
        self.assertFalse(self.client.get("/api/v1/strategy/pump-paper-v1/shadow-compare").json()["data"]["enabled"])

    def test_derive_habits_keeps_profiles_and_round8b(self):
        r = self.client.put(
            "/api/v1/strategy/pump-paper-v1/shadow-compare",
            json={
                "enabled": True,
                "derive_from_habits": True,
                "setup_seed_tags": ["lab-seed"],
                "auto_paper_orders": True,
            },
        )
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()["data"]
        self.assertTrue(data["enabled"])
        self.assertFalse(data["liveEnabled"])
        self.assertGreaterEqual(len(data["sets"]), 1)
        self.assertLessEqual(len(data["sets"]), 3)
        for spec in data["sets"]:
            params = spec.get("params") or {}
            self.assertFalse(any(str(k).startswith("progress") for k in params))
            self.assertIn("lab-seed", spec.get("setup_seed_tags") or [])
            self.assertEqual(spec.get("source"), "habit")
        self.assertEqual([w.model_dump() for w in list_watches()], self.watches_before)
        for w in list_watches():
            profile = habit_profile(w.watch_id, now_ms=NOW)
            if w.watch_id in self.profiles_before:
                self.assertIsNotNone(profile)
                self.assertEqual(profile.model_dump(), self.profiles_before[w.watch_id])  # type: ignore[union-attr]
        params = self.client.get("/api/v1/strategy/pump-paper-v1").json()["data"]["params"]
        self.assertEqual(params["progress_bps_min"], 1500)
        self.assertEqual(params["take_profit_pct"], 0.06)
        self.assertEqual(params["stop_loss_pct"], 0.05)
        self.assertEqual(params["max_hold_sec"], 120)
        self.assertFalse(params["auto_paper_orders"])


class TickIsolationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        os.environ["DATA_PROVIDER"] = "pumpfun_paper"
        os.environ["PUMP_PAPER_LOOP"] = "0"
        os.environ["PUMPFUN_DISCOVERY"] = "off"
        from app.providers import reset_provider

        reset_provider()
        reset_engine()
        reset_shadow_compare()
        reset_paper_ledger(wipe_store=True)
        reset_decision_log()
        reset_risk_gate()

    def tearDown(self):
        from app.providers import reset_provider

        reset_shadow_compare()
        reset_engine()
        reset_paper_ledger(wipe_store=True)
        reset_decision_log()
        os.environ["DATA_PROVIDER"] = "mock"
        reset_provider()

    async def test_tick_shadow_does_not_open_paper_position(self):
        apply_shadow_config(
            {
                "enabled": True,
                "sets": [
                    {
                        "id": "loose",
                        "min_trade_count_1m": 1,
                        "min_buy_sell_ratio_1m": 0.5,
                        "max_hold_sec": 30,
                    }
                ],
            }
        )
        engine = PumpPaperEngine()
        self.assertFalse(engine.params.auto_paper_orders)
        from app.providers import get_provider

        provider = get_provider()
        symbol = "PUMPDEMO/SOL"
        curve = provider._curves[symbol]  # type: ignore[attr-defined]
        # Deepen reserves so the shared 0.12 SOL clip stays inside the 75 bps buffer.
        curve.virtual_sol *= 20
        curve.virtual_token *= 20
        now = int(time.time() * 1000)
        curve.last_ms = now
        provider._trades[symbol] = [  # type: ignore[attr-defined]
            {
                "mint": curve.mint,
                "symbol": symbol,
                "ts": now - (i + 1) * 1_000,
                "side": "buy" if i < 10 else "sell",
                "price": 2.8e-5,
                "qty": 1000,
                "sol_amount": 0.4 if i < 10 else 0.05,
                "phase": "curve",
            }
            for i in range(12)
        ]
        await engine.tick()
        self.assertEqual(engine.positions, {})
        self.assertEqual(get_paper_journal().closed, [])
        self.assertTrue(any(d["action"] == "enter" and d["set_id"] == "loose" for d in shadow_decisions()))
        self.assertTrue(all("SHADOW" not in (r.signal_tags or []) for r in get_decision_log().rows))
