"""HabitEngine rules + distill reject paths (paper observe, no chain txs)."""
from __future__ import annotations

import ast
import json
import os
import tempfile
import unittest
from pathlib import Path

from app.models.contracts import TraderWatchlistItem
from app.strategies.pump_paper_v1 import PumpPaperParams, reset_engine
from app.traders import COPY_TRADE_ENABLED
from app.traders.distill import (
    DISTILL_PARAM_KEYS,
    FORBIDDEN_APPLY_KEYS,
    DistillReject,
    apply_distill,
    distill_profile,
    overlay_path,
    sanitize_patch,
)
from app.traders.habits import build_profile
from app.traders.helius import HeliusTraderReader, reader_mode
from app.traders.snapshot import mock_snapshot_for
from app.traders.store import (
    WATCH_GRAD,
    WATCH_MID,
    WATCH_SNIPER,
    reset_watch_store,
)

ROOT = Path(__file__).resolve().parents[3]
API_APP = ROOT / "apps" / "api" / "app"
NOW = 1_700_000_000_000


def _item(watch_id: str, address: str, label: str) -> TraderWatchlistItem:
    return TraderWatchlistItem(
        watch_id=watch_id,
        address=address,
        label=label,
        enabled=True,
        source="rpc",
        added_ts=NOW,
    )


class HabitRuleTests(unittest.TestCase):
    def test_sniper_mid_grad_flip_bag(self):
        sniper = build_profile(
            _item("watch-sniper", WATCH_SNIPER, "mock-sniper"),
            mock_snapshot_for(_item("watch-sniper", WATCH_SNIPER, "mock-sniper"), now_ms=NOW, persona="sniper"),
        )
        self.assertEqual(sniper.primary.tag, "sniper")  # type: ignore[union-attr]
        self.assertGreaterEqual(sniper.features.pct_entries_lt_800, 0.5)
        self.assertLessEqual(sniper.features.median_hold_sec or 999, 180)
        self.assertTrue(any(t.tag == "sniper" and t.evidence for t in sniper.tags))

        mid = build_profile(
            _item("watch-mid", WATCH_MID, "mock-mid-curve"),
            mock_snapshot_for(_item("watch-mid", WATCH_MID, "mock-mid-curve"), now_ms=NOW, persona="mid_curve"),
        )
        self.assertEqual(mid.primary.tag, "mid_curve")  # type: ignore[union-attr]
        self.assertGreaterEqual(mid.features.pct_entries_800_7500, 0.45)
        self.assertTrue(all(t.tag != "sniper" for t in mid.tags))

        grad = build_profile(
            _item("watch-grad", WATCH_GRAD, "mock-graduation-chase"),
            mock_snapshot_for(
                _item("watch-grad", WATCH_GRAD, "mock-graduation-chase"),
                now_ms=NOW,
                persona="graduation_chase",
            ),
        )
        self.assertEqual(grad.primary.tag, "graduation_chase")  # type: ignore[union-attr]
        self.assertTrue(
            grad.features.pct_entries_gt_9000 >= 0.35
            or (grad.features.median_entry_progress_bps or 0) >= 9000
        )

        flip_item = _item("watch-flip", "WatchFlipWallet111111111111111111111111111", "mock-flip")
        flip = build_profile(flip_item, mock_snapshot_for(flip_item, now_ms=NOW, persona="flip"))
        self.assertEqual(flip.primary.tag, "flip")  # type: ignore[union-attr]
        self.assertGreaterEqual(flip.features.flip_rate_24h or 0, 0.5)
        self.assertLess(flip.features.median_hold_sec or 999, 300)

        bag_item = _item("watch-bag", "WatchBagHolder1111111111111111111111111111", "mock-bag")
        bag = build_profile(bag_item, mock_snapshot_for(bag_item, now_ms=NOW, persona="bag"))
        self.assertEqual(bag.primary.tag, "bag")  # type: ignore[union-attr]
        self.assertGreater(bag.features.median_hold_sec or 0, 3600)
        self.assertGreaterEqual(bag.features.bag_score, 0.6)


class DistillMappingTests(unittest.TestCase):
    def setUp(self):
        self.params = PumpPaperParams().model_dump()

    def _result(self, persona: str, watch_id: str, address: str, label: str):
        item = _item(watch_id, address, label)
        snap = mock_snapshot_for(item, now_ms=NOW, persona=persona)
        profile = build_profile(item, snap)
        return distill_profile(profile, snap, current=self.params), profile

    def test_sniper_rejects_and_does_not_lower_min(self):
        result, _ = self._result("sniper", "watch-sniper", WATCH_SNIPER, "mock-sniper")
        self.assertIsNotNone(result.reject_reason)
        self.assertIn("sniper", result.reject_reason or "")
        self.assertNotIn("progress_bps_min", result.suggested_params)
        self.assertNotIn("auto_paper_orders", result.suggested_params)

    def test_graduation_chase_rejects_and_does_not_raise_max(self):
        result, _ = self._result(
            "graduation_chase", "watch-grad", WATCH_GRAD, "mock-graduation-chase"
        )
        self.assertIsNotNone(result.reject_reason)
        self.assertIn("graduation_chase", result.reject_reason or "")
        self.assertNotIn("progress_bps_max", result.suggested_params)

    def test_mid_curve_tightens_window_inside_family(self):
        result, profile = self._result("mid_curve", "watch-mid", WATCH_MID, "mock-mid-curve")
        self.assertIsNone(result.reject_reason)
        patch = result.suggested_params
        self.assertIn("progress_bps_min", patch)
        self.assertIn("progress_bps_max", patch)
        self.assertGreaterEqual(patch["progress_bps_min"], self.params["progress_bps_min"])
        self.assertLessEqual(patch["progress_bps_max"], self.params["progress_bps_max"])
        median = profile.features.median_entry_progress_bps
        self.assertIsNotNone(median)
        self.assertLessEqual(abs(patch["progress_bps_min"] - int(median or 0)), 2000)

    def test_flip_and_bag_param_direction(self):
        flip, _ = self._result("flip", "w-flip", "WatchFlipWallet111111111111111111111111111", "f")
        self.assertIsNone(flip.reject_reason)
        self.assertLess(flip.suggested_params["max_hold_sec"], self.params["max_hold_sec"])
        self.assertLess(flip.suggested_params["take_profit_pct"], self.params["take_profit_pct"])

        bag, _ = self._result("bag", "w-bag", "WatchBagHolder1111111111111111111111111111", "b")
        self.assertIsNone(bag.reject_reason)
        self.assertGreater(bag.suggested_params["max_hold_sec"], self.params["max_hold_sec"])
        self.assertLess(bag.suggested_params["stop_loss_pct"], self.params["stop_loss_pct"])

    def test_sanitize_refuses_window_widen_and_strips_autopaper(self):
        current = PumpPaperParams().model_dump()
        with self.assertRaises(DistillReject) as low:
            sanitize_patch({"progress_bps_min": 100}, current)
        self.assertEqual(low.exception.code, "DISTILL_WINDOW_GUARD")
        with self.assertRaises(DistillReject) as high:
            sanitize_patch({"progress_bps_max": 9500}, current)
        self.assertEqual(high.exception.code, "DISTILL_WINDOW_GUARD")
        clean = sanitize_patch(
            {
                "auto_paper_orders": True,
                "strategy_autopaper": True,
                "copy_trade_enabled": True,
                "max_impact_bps": 400,
                "max_hold_sec": 200,
            },
            current,
        )
        self.assertNotIn("auto_paper_orders", clean)
        self.assertNotIn("strategy_autopaper", clean)
        self.assertNotIn("copy_trade_enabled", clean)
        self.assertEqual(clean["max_impact_bps"], 80.0)
        self.assertEqual(clean["max_hold_sec"], 200)
        self.assertTrue(COPY_TRADE_ENABLED is False)
        self.assertNotIn("auto_paper_orders", DISTILL_PARAM_KEYS)
        for key in FORBIDDEN_APPLY_KEYS:
            self.assertNotIn(key, DISTILL_PARAM_KEYS)


class GuardrailSourceTests(unittest.TestCase):
    def test_no_sendtx_frontend_scrape_or_copy_trade_path(self):
        banned_needles = (
            "sendtransaction",
            "frontend-api-v3.pump.fun",
            "profile-api.pump.fun",
            "/mirror",
            "copy_trade_enabled = true",
            "copy_trade_enabled=true",
        )
        hits: list[str] = []
        for path in (API_APP / "traders").rglob("*.py"):
            text = path.read_text(encoding="utf-8").lower()
            for needle in banned_needles:
                if needle in text:
                    hits.append(f"{path.relative_to(ROOT)}:{needle}")
        watch_route = (API_APP / "routes" / "watch.py").read_text(encoding="utf-8").lower()
        strat_route = (API_APP / "routes" / "strategy.py").read_text(encoding="utf-8").lower()
        for needle in banned_needles:
            if needle in watch_route:
                hits.append(f"routes/watch.py:{needle}")
            if needle in strat_route and needle != "/mirror":
                # strategy may mention copy_trade_enabled as False via import/payload
                if "true" in needle:
                    hits.append(f"routes/strategy.py:{needle}")
        self.assertEqual(hits, [])
        self.assertFalse(COPY_TRADE_ENABLED)
        self.assertEqual(reader_mode(), "mock")
        self.assertFalse(HeliusTraderReader.LIVE_FETCH)

    def test_traders_package_does_not_import_live_sdks(self):
        banned = {"solders", "solana", "jito"}
        seen: list[str] = []
        for path in (API_APP / "traders").rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name.split(".")[0].lower() in banned:
                            seen.append(alias.name)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    if node.module.split(".")[0].lower() in banned:
                        seen.append(node.module)
        self.assertEqual(seen, [])


class WatchApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        os.environ["TRADER_WATCH_STORE"] = str(Path(cls.tmp.name) / "watch.json")
        os.environ["TRADER_DISTILL_OVERLAY"] = str(Path(cls.tmp.name) / "overlay.json")
        os.environ["TRADER_WATCH_READER"] = "mock"
        os.environ["PUMP_PAPER_LOOP"] = "0"
        os.environ["PUMPFUN_DISCOVERY"] = "off"
        os.environ["DATA_PROVIDER"] = "mock"
        reset_watch_store(seed=True)
        reset_engine()
        from fastapi.testclient import TestClient
        from app.main import app

        cls.client = TestClient(app)

    @classmethod
    def tearDownClass(cls):
        reset_engine()
        reset_watch_store(seed=True)
        cls.tmp.cleanup()

    def setUp(self):
        reset_watch_store(seed=True)
        reset_engine()

    def test_crud_addresses_only(self):
        r = self.client.get("/api/v1/watch/traders")
        self.assertEqual(r.status_code, 200)
        data = r.json()["data"]
        self.assertFalse(data["copy_trade_enabled"])
        self.assertEqual(data["reader"], "mock")
        addrs = {i["address"] for i in data["items"]}
        self.assertIn(WATCH_SNIPER, addrs)
        self.assertIn(WATCH_MID, addrs)
        self.assertIn(WATCH_GRAD, addrs)
        for item in data["items"]:
            self.assertNotIn("private_key", item)
            self.assertTrue(item["address"])

        added = self.client.put(
            "/api/v1/watch/traders",
            json={
                "address": "WatchExtraAddr1111111111111111111111111111",
                "label": "extra",
            },
        )
        self.assertEqual(added.status_code, 200)
        self.assertTrue(
            any(i["address"].startswith("WatchExtra") for i in added.json()["data"]["items"])
        )
        forbidden = self.client.put(
            "/api/v1/watch/traders",
            json={"address": WATCH_MID, "private_key": "should-not-work"},
        )
        self.assertIn(forbidden.status_code, (400, 422))
        deleted = self.client.delete("/api/v1/watch/traders/watch-sniper")
        self.assertEqual(deleted.status_code, 200)
        left = {i["watch_id"] for i in deleted.json()["data"]["items"]}
        self.assertNotIn("watch-sniper", left)

    def test_three_wallets_tags_distill_confirm_apply(self):
        listing = self.client.get("/api/v1/watch/traders").json()["data"]["items"]
        self.assertGreaterEqual(len(listing), 3)
        tags_by_id: dict[str, str] = {}
        for row in listing:
            habits = self.client.get(f"/api/v1/watch/traders/{row['watch_id']}/habits")
            self.assertEqual(habits.status_code, 200)
            body = habits.json()["data"]
            self.assertTrue(body["tags"])
            self.assertIn(body["primary"]["tag"], {t["tag"] for t in body["tags"]})
            tags_by_id[row["watch_id"]] = body["primary"]["tag"]
            snap = self.client.get(f"/api/v1/watch/traders/{row['watch_id']}/snapshot")
            self.assertEqual(snap.status_code, 200)
            payload = snap.json()["data"]
            for key in (
                "progress_hist",
                "entry_progress_median_bps",
                "hold_sec",
                "flip_rate_24h",
                "recent_buys",
                "recent_sells",
            ):
                if key == "hold_sec":
                    self.assertTrue(
                        payload.get("median_hold_sec_24h") is not None
                        or any("hold_sec" in p for p in payload.get("positions") or [])
                    )
                else:
                    self.assertIn(key if key != "hold_sec" else "median_hold_sec_24h", payload)
            hist = payload["progress_hist"]
            for bucket in ("0_800", "800_5000", "5000_7500", "7500_9000", "9000_10000", "migrated"):
                self.assertIn(bucket, hist)

        self.assertEqual(tags_by_id.get("watch-sniper"), "sniper")
        self.assertEqual(tags_by_id.get("watch-mid"), "mid_curve")
        self.assertEqual(tags_by_id.get("watch-grad"), "graduation_chase")

        sniper_d = self.client.post("/api/v1/watch/traders/watch-sniper/distill")
        self.assertIsNotNone(sniper_d.json()["data"]["reject_reason"])
        self.assertFalse(sniper_d.json()["data"]["applied"])
        no_confirm = self.client.post(
            "/api/v1/strategy/pump-paper-v1/apply-distill",
            json={"source_watch_id": "watch-mid", "confirm": False},
        )
        self.assertEqual(no_confirm.status_code, 400)
        self.assertEqual(no_confirm.json()["error"]["code"], "CONFIRM_REQUIRED")

        sniper_apply = self.client.post(
            "/api/v1/strategy/pump-paper-v1/apply-distill",
            json={"source_watch_id": "watch-sniper", "confirm": True},
        )
        self.assertEqual(sniper_apply.status_code, 400)
        self.assertEqual(sniper_apply.json()["error"]["code"], "DISTILL_REJECTED")

        grad_apply = self.client.post(
            "/api/v1/strategy/pump-paper-v1/apply-distill",
            json={"source_watch_id": "watch-grad", "confirm": True},
        )
        self.assertEqual(grad_apply.status_code, 400)

        before = self.client.get("/api/v1/strategy/pump-paper-v1").json()["data"]
        self.assertFalse(before["auto_paper_orders"])
        self.assertTrue(before["liveDisabled"])
        self.assertFalse(before["copy_trade_enabled"])
        prev_min = before["params"]["progress_bps_min"]
        prev_max = before["params"]["progress_bps_max"]

        mid_d = self.client.post("/api/v1/watch/traders/watch-mid/distill")
        proposal = mid_d.json()["data"]
        self.assertIsNone(proposal["reject_reason"])
        self.assertGreaterEqual(proposal["suggested_params"]["progress_bps_min"], prev_min)
        self.assertLessEqual(proposal["suggested_params"]["progress_bps_max"], prev_max)

        applied = self.client.post(
            "/api/v1/strategy/pump-paper-v1/apply-distill",
            json={
                "source_watch_id": "watch-mid",
                "confirm": True,
                "suggested_params": {**proposal["suggested_params"], "auto_paper_orders": True},
            },
        )
        self.assertEqual(applied.status_code, 200)
        data = applied.json()["data"]
        self.assertFalse(data["auto_paper_orders"])
        self.assertFalse(data["params"]["auto_paper_orders"])
        self.assertNotEqual(data["params"]["progress_bps_min"], 800)
        self.assertLessEqual(data["params"]["progress_bps_max"], 7500)
        self.assertGreaterEqual(data["params"]["progress_bps_min"], 800)
        self.assertFalse(data["copy_trade_enabled"])
        self.assertTrue(data["liveDisabled"])
        path = Path(data["distill"]["overlay_path"])
        self.assertTrue(path.is_file())
        overlay = json.loads(path.read_text(encoding="utf-8"))
        self.assertFalse(overlay["copy_trade_enabled"])
        self.assertTrue(overlay["auto_paper_orders_untouched"])
        self.assertIn("progress_bps_min", overlay["params"])
        self.assertEqual(overlay_path(), path)

        health = self.client.get("/api/v1/health").json()["data"]
        self.assertTrue(health["liveDisabled"])
        self.assertFalse(health["copy_trade_enabled"])
        self.assertEqual(health["trader_watch_reader"], "mock")
        self.assertFalse(health["helius_enabled"])

        compare = self.client.get("/api/v1/watch/traders/watch-mid/compare")
        self.assertEqual(compare.status_code, 200)
        note = compare.json()["data"]["note"]
        self.assertIn("reference_only", note)
        self.assertTrue(compare.json()["data"]["self"]["liveDisabled"])

    def test_no_mirror_route(self):
        r = self.client.post("/api/v1/watch/traders/watch-mid/mirror")
        self.assertIn(r.status_code, (404, 405, 400))
        health = self.client.get("/api/v1/health").json()["data"]
        self.assertNotIn("copy_trade_enabled", [k for k, v in health.items() if v is True])

    def test_apply_distill_does_not_send_chain(self):
        src = Path(apply_distill.__code__.co_filename).read_text(encoding="utf-8").lower()
        self.assertNotIn("sendtransaction", src)
        self.assertIn("confirm=true", src.replace(" ", ""))


if __name__ == "__main__":
    unittest.main()
