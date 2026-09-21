"""Live adapter: liveEnabled default false; LiveLimits isolated; paper journal unmixed."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from app.live.gate import (
    ENV_KEYPAIR_PATH,
    ENV_LIVE_ARMED,
    ENV_LIVE_CONFIRMED,
    ENV_LIVE_DISABLED,
    ENV_LIVE_ENABLED,
    ENV_MAX_DAY_LOSS_PCT,
    ENV_MAX_NOTIONAL_SOL,
    ENV_MAX_OPEN_MINTS,
    LOCKED_MAX_DAY_LOSS_PCT,
    LOCKED_MAX_NOTIONAL_SOL,
    LOCKED_MAX_OPEN_MINTS,
    REASON_LIMITS_MISSING,
    REASON_LIVE_DISABLED,
    REASON_NO_KEYPAIR,
    evaluate,
    reset_live_state,
    try_set_armed,
    try_set_disabled,
    try_set_enabled_with_confirm,
)
from app.live.intent import BUY_METHOD, PUMP_SDK_PACKAGE, SELL_METHOD, build_intent_for_order
from app.live.ledger import LiveTradeJournal, get_live_ledger, reset_live_ledger
from app.live.send import send_allowed
from app.live.signer import LocalSigner
from app.models.contracts import (
    AccountCtx,
    Fill,
    LiquidityCtx,
    SignalOut,
    SizeIn,
    StrategyContext,
)
from app.paper.broker import reset_paper_broker
from app.paper.guard import live_disabled as paper_live_disabled
from app.paper.guard import live_execution_blocked
from app.paper.ledger import PaperLedger, reset_paper_ledger, summarize
from app.providers import reset_provider
from app.risk.gate import REASON, RiskGate, RiskLimits, reset_risk_gate
from app.strategies.pump_paper_v1 import reset_engine

ROOT = Path(__file__).resolve().parents[3]
API_APP = ROOT / "apps" / "api" / "app"
LIVE_PY = API_APP / "live"

_LIVE_ENV = (
    ENV_LIVE_DISABLED,
    ENV_LIVE_ENABLED,
    ENV_LIVE_ARMED,
    ENV_LIVE_CONFIRMED,
    ENV_KEYPAIR_PATH,
    ENV_MAX_NOTIONAL_SOL,
    ENV_MAX_DAY_LOSS_PCT,
    ENV_MAX_OPEN_MINTS,
    "LIVE_TRADING",
)

LOCKED_LIMITS = {
    "max_notional_sol": LOCKED_MAX_NOTIONAL_SOL,
    "max_day_loss_pct": LOCKED_MAX_DAY_LOSS_PCT,
    "max_open_mints": LOCKED_MAX_OPEN_MINTS,
}


def _clear_live_env() -> None:
    for key in _LIVE_ENV:
        os.environ.pop(key, None)


def _dummy_keypair(dirpath: str, marker: int = 173) -> str:
    path = Path(dirpath) / "solana-test-keypair.json"
    blob = [marker] * 32 + [(marker + 1) % 256] * 32
    path.write_text(json.dumps(blob), encoding="utf-8")
    return str(path)


def _arm(tmp: str) -> None:
    os.environ[ENV_LIVE_DISABLED] = "false"
    os.environ[ENV_LIVE_ARMED] = "true"
    os.environ[ENV_KEYPAIR_PATH] = _dummy_keypair(tmp)
    reset_live_state()


class LiveGateDefaultTests(unittest.TestCase):
    def setUp(self):
        _clear_live_env()
        reset_live_state()

    def tearDown(self):
        _clear_live_env()
        reset_live_state()

    def test_live_disabled_by_default_with_locked_caps(self):
        st = evaluate()
        self.assertFalse(st.live_enabled)
        self.assertFalse(st.live_confirmed)
        self.assertTrue(st.live_disabled)
        self.assertFalse(st.live_armed)
        self.assertFalse(st.live_send_wired)
        self.assertIn(REASON_LIVE_DISABLED, st.reasons)
        self.assertIn(REASON_NO_KEYPAIR, st.reasons)
        self.assertNotIn(REASON_LIMITS_MISSING, st.reasons)
        self.assertFalse(st.armed)
        self.assertFalse(st.keypair_configured)
        self.assertEqual(st.as_dict()["keypairMounted"], "no")
        self.assertEqual(st.limits.as_dict(), LOCKED_LIMITS)
        self.assertTrue(paper_live_disabled())
        self.assertFalse(send_allowed())

    def test_zero_env_falls_back_to_locked_caps(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ[ENV_LIVE_DISABLED] = "false"
            os.environ[ENV_LIVE_ARMED] = "true"
            os.environ[ENV_KEYPAIR_PATH] = _dummy_keypair(tmp)
            os.environ[ENV_MAX_NOTIONAL_SOL] = "0"
            os.environ[ENV_MAX_DAY_LOSS_PCT] = "0"
            os.environ[ENV_MAX_OPEN_MINTS] = "0"
            st = evaluate()
            self.assertEqual(st.limits.as_dict(), LOCKED_LIMITS)
            self.assertNotIn(REASON_LIMITS_MISSING, st.reasons)
            self.assertTrue(st.armed)
            self.assertTrue(st.live_enabled)
            self.assertTrue(st.live_confirmed)

    def test_env_cannot_loosen_locked_caps(self):
        os.environ[ENV_MAX_NOTIONAL_SOL] = "9"
        os.environ[ENV_MAX_DAY_LOSS_PCT] = "0.5"
        os.environ[ENV_MAX_OPEN_MINTS] = "99"
        st = evaluate()
        self.assertEqual(st.limits.max_notional_sol, LOCKED_MAX_NOTIONAL_SOL)
        self.assertEqual(st.limits.max_day_loss_pct, LOCKED_MAX_DAY_LOSS_PCT)
        self.assertEqual(st.limits.max_open_mints, LOCKED_MAX_OPEN_MINTS)

    def test_live_enabled_without_confirm_or_keypair_is_live_disabled(self):
        os.environ[ENV_LIVE_ENABLED] = "true"
        st = evaluate()
        self.assertTrue(st.live_enabled)
        self.assertFalse(st.live_confirmed)
        self.assertIn(REASON_LIVE_DISABLED, st.reasons)
        self.assertIn(REASON_NO_KEYPAIR, st.reasons)
        self.assertFalse(st.armed)

    def test_missing_keypair_cannot_arm(self):
        os.environ[ENV_LIVE_DISABLED] = "false"
        os.environ[ENV_LIVE_ARMED] = "true"
        st = evaluate()
        self.assertFalse(st.armed)
        self.assertIn(REASON_NO_KEYPAIR, st.reasons)
        self.assertIn(REASON_LIVE_DISABLED, st.reasons)
        self.assertNotIn(REASON_LIMITS_MISSING, st.reasons)
        ok_arm, st2 = try_set_armed(True)
        self.assertFalse(ok_arm)
        self.assertIn(REASON_NO_KEYPAIR, st2.reasons)
        self.assertIn(REASON_LIVE_DISABLED, st2.reasons)

    def test_live_armed_defaults_false_even_with_keypair(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ[ENV_LIVE_DISABLED] = "false"
            os.environ[ENV_KEYPAIR_PATH] = _dummy_keypair(tmp)
            st = evaluate()
            self.assertFalse(st.armed_flag)
            self.assertFalse(st.live_confirmed)
            self.assertIn(REASON_LIVE_DISABLED, st.reasons)
            self.assertFalse(st.armed)
            self.assertEqual(st.limits.as_dict(), LOCKED_LIMITS)

    def test_settings_cannot_disable_switch_without_keypair(self):
        ok, st = try_set_disabled(False)
        self.assertFalse(ok)
        self.assertTrue(st.disabled_switch)
        with tempfile.TemporaryDirectory() as tmp:
            os.environ[ENV_KEYPAIR_PATH] = _dummy_keypair(tmp)
            ok2, st2 = try_set_disabled(False)
            self.assertTrue(ok2)
            self.assertFalse(st2.disabled_switch)
            self.assertFalse(st2.armed)
            self.assertIn(REASON_LIVE_DISABLED, st2.reasons)

    def test_enabled_with_confirm_requires_keypair(self):
        ok, st = try_set_enabled_with_confirm(True, confirmed=True)
        self.assertFalse(ok)
        self.assertIn(REASON_LIVE_DISABLED, st.reasons)
        with tempfile.TemporaryDirectory() as tmp:
            os.environ[ENV_KEYPAIR_PATH] = _dummy_keypair(tmp)
            ok2, st2 = try_set_enabled_with_confirm(True, confirmed=True)
            self.assertTrue(ok2)
            self.assertTrue(st2.live_enabled)
            self.assertTrue(st2.live_confirmed)
            self.assertTrue(st2.armed)
            self.assertNotIn(REASON_LIVE_DISABLED, st2.reasons)

    def test_paper_risk_limits_do_not_hold_live_caps(self):
        fields = set(RiskLimits.__dataclass_fields__)
        self.assertNotIn("max_notional_sol", fields)
        self.assertNotIn("live_max_day_loss_pct", fields)
        self.assertNotIn("max_open_mints", fields)


class LocalSignerTests(unittest.TestCase):
    def test_missing_path(self):
        st = LocalSigner().inspect(None)
        self.assertFalse(st.ok)
        self.assertEqual(st.reason, REASON_NO_KEYPAIR)

    def test_inspect_does_not_log_secret_bytes(self):
        marker = 199
        with tempfile.TemporaryDirectory() as tmp:
            path = _dummy_keypair(tmp, marker=marker)
            signer = LocalSigner()
            with self.assertLogs("auu.live.signer", level="INFO") as cm:
                st = signer.inspect(path)
            self.assertTrue(st.ok)
            blob = "\n".join(cm.output)
            self.assertNotIn(str(marker), blob)
            self.assertNotIn(json.dumps([marker] * 32), blob)
            self.assertIn("present", blob.lower())

    def test_sign_message_not_implemented(self):
        with self.assertRaises(RuntimeError):
            LocalSigner().sign_message(b"x")


class IntentAndSendGateTests(unittest.TestCase):
    def test_intent_uses_official_pump_sdk_methods_only(self):
        buy = build_intent_for_order(side="buy", mint="Mint111", qty_or_notional=0.25)
        sell = build_intent_for_order(side="sell", mint="Mint111", qty_or_notional=10)
        self.assertEqual(buy["sdk"], PUMP_SDK_PACKAGE)
        self.assertEqual(buy["method"], BUY_METHOD)
        self.assertTrue(buy["unsigned"])
        self.assertFalse(buy["sent"])
        self.assertEqual(sell["method"], SELL_METHOD)
        self.assertFalse(send_allowed())


class RiskGateLiveExtrasTests(unittest.TestCase):
    def setUp(self):
        _clear_live_env()
        reset_live_state()
        reset_risk_gate()

    def tearDown(self):
        _clear_live_env()
        reset_live_state()
        reset_risk_gate()

    def test_live_extra_tags_registered(self):
        for tag in ("LIMITS_MISSING", "LIVE_DISABLED", "NO_KEYPAIR", "MAX_OPEN_MINTS"):
            self.assertIn(tag, REASON)

    def test_check_live_tags_live_disabled_when_not_enabled(self):
        gate = RiskGate()
        ctx = StrategyContext(
            symbol="PUMPDEMO/SOL",
            ts=1,
            account=AccountCtx(equity=10_000.0, day_pnl=0.0),
            liquidity=LiquidityCtx(spread_bps=10, adv_usd=1_000_000),
        )
        risk = gate.check_live(ctx, SignalOut(side="long"), SizeIn(target_notional=0.5))
        self.assertFalse(risk.allow)
        self.assertIn("LIVE_DISABLED", risk.tags)
        self.assertIn("NO_KEYPAIR", risk.tags)

    def test_check_live_fail_closed_when_limits_zeroed(self):
        gate = RiskGate()
        ctx = StrategyContext(
            symbol="PUMPDEMO/SOL",
            ts=1,
            account=AccountCtx(),
            liquidity=LiquidityCtx(),
        )
        risk = gate.check_live(
            ctx,
            SignalOut(side="long"),
            SizeIn(target_notional=0.05),
            live_limits={"max_notional_sol": 0, "max_day_loss_pct": 0, "max_open_mints": 0},
        )
        self.assertFalse(risk.allow)
        self.assertIn("LIMITS_MISSING", risk.tags)
        self.assertIn("LIVE_DISABLED", risk.tags)

    def test_check_live_enforces_locked_notional_open_mints_and_day_loss(self):
        gate = RiskGate()
        with tempfile.TemporaryDirectory() as tmp:
            _arm(tmp)
            ctx = StrategyContext(
                symbol="PUMPDEMO/SOL",
                ts=1,
                account=AccountCtx(equity=100.0, day_pnl=0.0),
                liquidity=LiquidityCtx(spread_bps=10, adv_usd=1_000_000),
                meta={"open_mints": 10},
            )
            over = gate.check_live(
                ctx, SignalOut(side="long"), SizeIn(target_notional=1.01)
            )
            self.assertFalse(over.allow)
            self.assertIn("MAX_NOTIONAL", over.tags)
            self.assertNotIn("LIVE_DISABLED", over.tags)
            caps = gate.check_live(
                ctx, SignalOut(side="long"), SizeIn(target_notional=0.5)
            )
            self.assertFalse(caps.allow)
            self.assertIn("MAX_OPEN_MINTS", caps.tags)
            loss_ctx = StrategyContext(
                symbol="PUMPDEMO/SOL",
                ts=1,
                account=AccountCtx(equity=100.0, day_pnl=-4.6),
                liquidity=LiquidityCtx(spread_bps=10, adv_usd=1_000_000),
            )
            loss = gate.check_live(
                loss_ctx, SignalOut(side="long"), SizeIn(target_notional=0.5)
            )
            self.assertFalse(loss.allow)
            self.assertIn("DAY_LOSS_BREAKER", loss.tags)

    def test_check_live_does_not_reuse_paper_day_loss(self):
        gate = RiskGate()
        gate.limits.max_day_loss_pct = 0.03
        with tempfile.TemporaryDirectory() as tmp:
            _arm(tmp)
            ctx = StrategyContext(
                symbol="PUMPDEMO/SOL",
                ts=1,
                account=AccountCtx(equity=100.0, day_pnl=-4.0),
                liquidity=LiquidityCtx(spread_bps=10, adv_usd=1_000_000),
            )
            # 4.0% is below live 4.5% and would trip paper 3% if reused.
            risk = gate.check_live(ctx, SignalOut(side="long"), SizeIn(target_notional=0.5))
            self.assertTrue(risk.allow)
            paper = gate.check(ctx, SignalOut(side="long"), SizeIn(target_notional=50))
            self.assertFalse(paper.allow)
            self.assertIn("DAY_LOSS_BREAKER", paper.tags)

    def test_paper_check_keeps_five_pct_day_loss(self):
        gate = RiskGate()
        self.assertEqual(gate.limits.max_day_loss_pct, 0.05)
        ctx = StrategyContext(
            symbol="PUMPDEMO/SOL",
            ts=1,
            account=AccountCtx(equity=10_000.0, day_pnl=-460.0),
            liquidity=LiquidityCtx(spread_bps=10, adv_usd=1_000_000),
        )
        risk = gate.check(ctx, SignalOut(side="long"), SizeIn(target_notional=50))
        self.assertTrue(risk.allow)


class LiveLedgerIsolationTests(unittest.TestCase):
    def setUp(self):
        reset_paper_ledger()
        reset_live_ledger()

    def tearDown(self):
        reset_paper_ledger()
        reset_live_ledger()

    def test_paper_journal_ignores_live_tagged_fills(self):
        paper = PaperLedger()
        paper.record_fill("A/SOL", Fill(ts=1, price=1.0, qty=1.0, tag="paper-trade-ui"))
        paper.record_fill("A/SOL", Fill(ts=2, price=1.2, qty=-1.0, tag="paper-trade-ui"))
        ignored = paper.record_fill("B/SOL", Fill(ts=3, price=1.0, qty=1.0, tag="live-manual"))
        self.assertEqual(ignored, [])
        stats = summarize(paper.closed)
        self.assertEqual(stats["mode"], "paper")
        self.assertEqual(stats["n_trades"], 1)
        self.assertAlmostEqual(stats["win_rate"], 1.0)

    def test_summarize_drops_source_live_even_if_injected(self):
        paper = PaperLedger()
        paper.record_fill("A/SOL", Fill(ts=1, price=1.0, qty=1.0, tag="manual"))
        closed = paper.record_fill("A/SOL", Fill(ts=2, price=0.5, qty=-1.0, tag="manual"))
        closed[0].source = "live"
        stats = summarize(paper.closed)
        self.assertEqual(stats["n_trades"], 0)
        self.assertIsNone(stats["win_rate"])
        self.assertEqual(stats["mode"], "paper")

    def test_live_ledger_source_is_live_and_separate(self):
        live = get_live_ledger()
        live.record_fill("A/SOL", Fill(ts=1, price=1.0, qty=1.0, tag="live"))
        closed = live.record_fill("A/SOL", Fill(ts=2, price=1.5, qty=-1.0, tag="live"))
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0].source, "live")
        paper = PaperLedger()
        paper.record_fill("A/SOL", Fill(ts=1, price=1.0, qty=1.0, tag="paper"))
        paper.record_fill("A/SOL", Fill(ts=2, price=0.8, qty=-1.0, tag="paper"))
        paper_stats = summarize(paper.closed)
        self.assertEqual(paper_stats["n_trades"], 1)
        self.assertAlmostEqual(paper_stats["win_rate"], 0.0)
        self.assertEqual(LiveTradeJournal().closed, [])


class LiveRouteAndPaperTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["DATA_PROVIDER"] = "pumpfun_paper"
        os.environ["PUMPFUN_DISCOVERY"] = "off"
        os.environ["PUMP_PAPER_LOOP"] = "0"
        _clear_live_env()
        reset_live_state()
        reset_live_ledger()
        reset_provider()
        reset_engine()
        reset_risk_gate()
        reset_paper_broker()
        reset_paper_ledger()
        from fastapi.testclient import TestClient
        from app.main import app

        cls.client = TestClient(app)

    @classmethod
    def tearDownClass(cls):
        _clear_live_env()
        reset_live_state()
        reset_live_ledger()
        os.environ["DATA_PROVIDER"] = "mock"
        reset_provider()
        reset_engine()
        reset_risk_gate()
        reset_paper_broker()
        reset_paper_ledger()

    def setUp(self):
        _clear_live_env()
        reset_live_state()
        reset_live_ledger()
        reset_risk_gate()
        reset_paper_broker()

    def tearDown(self):
        _clear_live_env()
        reset_live_state()
        reset_live_ledger()

    def test_health_live_disabled_default(self):
        r = self.client.get("/api/v1/health")
        self.assertEqual(r.status_code, 200)
        data = r.json()["data"]
        self.assertFalse(data["liveEnabled"])
        self.assertFalse(data["liveConfirmed"])
        self.assertTrue(data["liveDisabled"])
        self.assertFalse(data["liveArmed"])
        self.assertFalse(data["liveSendWired"])
        self.assertIn(REASON_LIVE_DISABLED, data["liveReasons"])
        self.assertIn(REASON_NO_KEYPAIR, data["liveReasons"])
        self.assertNotIn(REASON_LIMITS_MISSING, data["liveReasons"])
        self.assertFalse(data["keypairConfigured"])
        self.assertEqual(data["keypairMounted"], "no")
        self.assertEqual(data["liveLimits"]["max_notional_sol"], LOCKED_MAX_NOTIONAL_SOL)
        self.assertEqual(data["liveLimits"]["max_day_loss_pct"], LOCKED_MAX_DAY_LOSS_PCT)
        self.assertEqual(data["liveLimits"]["max_open_mints"], LOCKED_MAX_OPEN_MINTS)

    def test_live_orders_403_when_disabled(self):
        body = {
            "ctx": {
                "symbol": "PUMPDEMO/SOL",
                "ts": 1_700_000_000_000,
                "tick": {"mid": 0.00003},
                "liquidity": {"spread_bps": 20, "adv_usd": 100000},
            },
            "intent": {"side": "buy", "order_type": "market", "qty_or_notional": 0.05, "client_tag": "live"},
        }
        r = self.client.post("/api/v1/live/orders", json=body)
        self.assertEqual(r.status_code, 403)
        err = r.json()["error"]
        self.assertIn(err["code"], {REASON_LIVE_DISABLED, REASON_NO_KEYPAIR})
        self.assertIn(REASON_LIVE_DISABLED, err["reasons"])
        self.assertIn(REASON_LIVE_DISABLED, err["tags"])
        self.assertIn(REASON_NO_KEYPAIR, err["reasons"])
        self.assertNotIn(REASON_LIMITS_MISSING, err["reasons"])
        self.assertIn(REASON_LIVE_DISABLED, err["risk"]["tags"])

    def test_arm_without_keypair_403(self):
        r = self.client.put("/api/v1/live/arm", json={"armed": True})
        self.assertEqual(r.status_code, 403)
        self.assertIn(REASON_NO_KEYPAIR, r.json()["error"]["reasons"])
        self.assertIn(REASON_LIVE_DISABLED, r.json()["error"]["reasons"])
        self.assertNotIn(REASON_LIMITS_MISSING, r.json()["error"]["reasons"])

    def test_enabled_without_confirm_403(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ[ENV_KEYPAIR_PATH] = _dummy_keypair(tmp)
            reset_live_state()
            r = self.client.put("/api/v1/live/enabled", json={"liveEnabled": True, "confirmed": False})
            self.assertEqual(r.status_code, 403)
            self.assertIn(REASON_LIVE_DISABLED, r.json()["error"]["reasons"])

    def test_armed_stub_does_not_fill(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ[ENV_LIVE_DISABLED] = "false"
            os.environ[ENV_LIVE_ARMED] = "true"
            os.environ[ENV_KEYPAIR_PATH] = _dummy_keypair(tmp)
            reset_live_state()
            st = self.client.get("/api/v1/live/status").json()["data"]
            self.assertTrue(st["liveArmed"])
            self.assertTrue(st["liveEnabled"])
            self.assertTrue(st["liveConfirmed"])
            self.assertTrue(st["liveDisabled"])
            self.assertFalse(st["sendEnabled"])
            self.assertEqual(st["keypairMounted"], "yes")
            self.assertEqual(st["limits"]["max_notional_sol"], 1.0)
            self.assertEqual(st["limits"]["max_day_loss_pct"], 0.045)
            self.assertEqual(st["limits"]["max_open_mints"], 10)
            body = {
                "ctx": {
                    "symbol": "PUMPDEMO/SOL",
                    "ts": 1_700_000_000_000,
                    "tick": {"mid": 0.00003},
                    "liquidity": {"spread_bps": 20, "adv_usd": 100000},
                    "account": {"equity": 10, "day_pnl": 0},
                },
                "intent": {
                    "side": "buy",
                    "order_type": "market",
                    "qty_or_notional": 0.05,
                    "client_tag": "live",
                    "max_slippage_bps": 150,
                },
            }
            r = self.client.post("/api/v1/live/orders", json=body)
            self.assertEqual(r.status_code, 200)
            data = r.json()["data"]
            self.assertEqual(data["fills"], [])
            self.assertEqual(data["venue"], "live")
            self.assertIn("LIVE_STUB", data["reject"]["tags"])
            self.assertEqual(data["intent"]["sdk"], "@pump-fun/pump-sdk")
            self.assertEqual(data["intent"]["method"], "buyInstructions")
            self.assertFalse(data["intent"]["sent"])
            ledger = self.client.get("/api/v1/live/ledger").json()["data"]
            self.assertEqual(ledger["mode"], "live")
            self.assertTrue(ledger["empty"])
            self.assertFalse(ledger["mixedIntoPaper"])

    def test_paper_pipeline_still_fills(self):
        blocked, why = live_execution_blocked()
        self.assertFalse(blocked, why)
        r = self.client.post(
            "/api/v1/pipeline/decide-and-fill",
            json={"symbol": "PUMPDEMO/SOL", "side": "buy", "notional": 0.1},
        )
        self.assertEqual(r.status_code, 200)
        data = r.json()["data"]
        self.assertTrue(data["risk"]["allow"])
        self.assertGreaterEqual(len(data["fills"]), 1)
        self.assertNotIn("reject", data)
        stats = self.client.get("/api/v1/strategy/pump-paper-v1/stats").json()["data"]
        self.assertEqual(stats["mode"], "paper")
        self.assertTrue(stats["liveDisabled"])
        for row in stats.get("journal") or []:
            self.assertNotEqual(row.get("source"), "live")

    def test_put_limits_cannot_change_locked_caps(self):
        r = self.client.put(
            "/api/v1/live/limits",
            json={"max_notional_sol": 9, "max_day_loss_pct": 0.5, "max_open_mints": 99},
        )
        self.assertEqual(r.status_code, 200)
        data = r.json()["data"]
        self.assertTrue(data["limitsLocked"])
        self.assertEqual(data["limits"]["max_notional_sol"], LOCKED_MAX_NOTIONAL_SOL)
        self.assertEqual(data["limits"]["max_day_loss_pct"], LOCKED_MAX_DAY_LOSS_PCT)
        self.assertEqual(data["limits"]["max_open_mints"], LOCKED_MAX_OPEN_MINTS)
        self.assertFalse(data["liveArmed"])
        self.assertFalse(data["liveEnabled"])


class NoCommittedKeypairTests(unittest.TestCase):
    def test_gitignore_bans_keypair_json(self):
        gi = (ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("keypair", gi.lower())
        self.assertIn("id.json", gi)

    def test_git_does_not_track_keypair_json(self):
        import subprocess

        tracked = subprocess.check_output(
            ["git", "ls-files"], cwd=ROOT, text=True, encoding="utf-8"
        ).splitlines()
        bad: list[str] = []
        for rel in tracked:
            name = rel.replace("\\", "/").lower()
            base = name.rsplit("/", 1)[-1]
            if "keypair" in base and base.endswith(".json"):
                bad.append(rel)
            if base == "id.json":
                bad.append(rel)
            if base.endswith(".json") and "package" not in base and "tsconfig" not in base:
                path = ROOT / rel
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if (
                    isinstance(data, list)
                    and len(data) in (32, 64)
                    and all(isinstance(x, int) and 0 <= x <= 255 for x in data)
                ):
                    bad.append(rel)
        self.assertEqual(bad, [])

    def test_live_python_has_no_chain_submit_or_sniper(self):
        banned = ("sendtransaction", "jito", "sniper", "searcher-keypair", "keypair.from")
        hits: list[str] = []
        for path in LIVE_PY.rglob("*.py"):
            text = path.read_text(encoding="utf-8").lower()
            for word in banned:
                if word in text:
                    hits.append(f"{path.relative_to(ROOT)}:{word}")
        self.assertEqual(hits, [])

    def test_live_python_does_not_load_private_key_env_strings(self):
        needle = "PRIVATE" + "_KEY"
        hits: list[str] = []
        for path in LIVE_PY.rglob("*.py"):
            if needle in path.read_text(encoding="utf-8"):
                hits.append(str(path.relative_to(ROOT)))
        self.assertEqual(hits, [])

    def test_settings_has_no_private_key_input(self):
        settings = (ROOT / "apps" / "web" / "src" / "pages" / "SettingsPage" / "SettingsPage.tsx").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("password", settings.lower())
        self.assertNotIn("private key", settings.lower())
        self.assertIn("mounted", settings.lower())
        self.assertIn("Confirm liveEnabled", settings)
        self.assertIn("LIVE_DISABLED", settings)


if __name__ == "__main__":
    unittest.main()
