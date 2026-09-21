"""Live adapter: disabled by default; cannot arm without limits + local keypair."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from app.live.gate import (
    ENV_KEYPAIR_PATH,
    ENV_LIVE_ARMED,
    ENV_LIVE_DISABLED,
    ENV_MAX_DAY_LOSS,
    ENV_MAX_NOTIONAL_SOL,
    ENV_MAX_OPEN_MINTS,
    REASON_LIMITS_MISSING,
    REASON_LIVE_DISABLED,
    REASON_NO_KEYPAIR,
    evaluate,
    reset_live_state,
    set_limits,
    try_set_armed,
    try_set_disabled,
)
from app.live.signer import LocalSigner
from app.models.contracts import (
    AccountCtx,
    LiquidityCtx,
    SignalOut,
    SizeIn,
    StrategyContext,
)
from app.paper.broker import reset_paper_broker
from app.paper.guard import live_disabled as paper_live_disabled
from app.paper.guard import live_execution_blocked
from app.paper.ledger import reset_paper_ledger
from app.providers import reset_provider
from app.risk.gate import REASON, RiskGate, reset_risk_gate
from app.strategies.pump_paper_v1 import reset_engine

ROOT = Path(__file__).resolve().parents[3]
API_APP = ROOT / "apps" / "api" / "app"
LIVE_PY = API_APP / "live"

_LIVE_ENV = (
    ENV_LIVE_DISABLED,
    ENV_LIVE_ARMED,
    ENV_KEYPAIR_PATH,
    ENV_MAX_NOTIONAL_SOL,
    ENV_MAX_DAY_LOSS,
    ENV_MAX_OPEN_MINTS,
    "LIVE_TRADING",
)


def _clear_live_env() -> None:
    for key in _LIVE_ENV:
        os.environ.pop(key, None)


def _dummy_keypair(dirpath: str, marker: int = 173) -> str:
    path = Path(dirpath) / "solana-test-keypair.json"
    blob = [marker] * 32 + [(marker + 1) % 256] * 32
    path.write_text(json.dumps(blob), encoding="utf-8")
    return str(path)


class LiveGateDefaultTests(unittest.TestCase):
    def setUp(self):
        _clear_live_env()
        reset_live_state()

    def tearDown(self):
        _clear_live_env()
        reset_live_state()

    def test_live_disabled_by_default(self):
        st = evaluate()
        self.assertTrue(st.live_disabled)
        self.assertFalse(st.live_armed)
        self.assertFalse(st.live_send_wired)
        self.assertIn(REASON_LIVE_DISABLED, st.reasons)
        self.assertIn(REASON_NO_KEYPAIR, st.reasons)
        self.assertIn(REASON_LIMITS_MISSING, st.reasons)
        self.assertFalse(st.armed)
        self.assertFalse(st.keypair_configured)
        self.assertEqual(
            st.limits.as_dict(),
            {"max_notional_sol": None, "max_day_loss": None, "max_open_mints": None},
        )
        self.assertTrue(paper_live_disabled())

    def test_missing_limits_cannot_arm(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ[ENV_LIVE_DISABLED] = "false"
            os.environ[ENV_LIVE_ARMED] = "true"
            os.environ[ENV_KEYPAIR_PATH] = _dummy_keypair(tmp)
            st = evaluate()
            self.assertFalse(st.armed)
            self.assertIn(REASON_LIMITS_MISSING, st.reasons)
            self.assertNotIn(REASON_NO_KEYPAIR, st.reasons)
            ok_arm, st2 = try_set_armed(True)
            self.assertFalse(ok_arm)
            self.assertIn(REASON_LIMITS_MISSING, st2.reasons)

    def test_zero_limits_cannot_arm(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ[ENV_LIVE_DISABLED] = "false"
            os.environ[ENV_LIVE_ARMED] = "true"
            os.environ[ENV_KEYPAIR_PATH] = _dummy_keypair(tmp)
            os.environ[ENV_MAX_NOTIONAL_SOL] = "0"
            os.environ[ENV_MAX_DAY_LOSS] = "0"
            os.environ[ENV_MAX_OPEN_MINTS] = "0"
            st = evaluate()
            self.assertIn(REASON_LIMITS_MISSING, st.reasons)
            self.assertFalse(st.armed)

    def test_missing_keypair_cannot_arm(self):
        os.environ[ENV_LIVE_DISABLED] = "false"
        os.environ[ENV_LIVE_ARMED] = "true"
        os.environ[ENV_MAX_NOTIONAL_SOL] = "0.1"
        os.environ[ENV_MAX_DAY_LOSS] = "0.5"
        os.environ[ENV_MAX_OPEN_MINTS] = "2"
        st = evaluate()
        self.assertFalse(st.armed)
        self.assertIn(REASON_NO_KEYPAIR, st.reasons)
        ok_arm, st2 = try_set_armed(True)
        self.assertFalse(ok_arm)
        self.assertIn(REASON_NO_KEYPAIR, st2.reasons)

    def test_live_armed_defaults_false_even_with_keypair_and_limits(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ[ENV_LIVE_DISABLED] = "false"
            os.environ[ENV_KEYPAIR_PATH] = _dummy_keypair(tmp)
            os.environ[ENV_MAX_NOTIONAL_SOL] = "0.1"
            os.environ[ENV_MAX_DAY_LOSS] = "0.5"
            os.environ[ENV_MAX_OPEN_MINTS] = "2"
            st = evaluate()
            self.assertFalse(st.armed_flag)
            self.assertIn(REASON_LIVE_DISABLED, st.reasons)
            self.assertFalse(st.armed)

    def test_settings_cannot_disable_switch_without_limits(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ[ENV_KEYPAIR_PATH] = _dummy_keypair(tmp)
            ok, st = try_set_disabled(False)
            self.assertFalse(ok)
            self.assertTrue(st.disabled_switch)
            set_limits(max_notional_sol=0.1, max_day_loss=0.25, max_open_mints=1, present={"max_notional_sol", "max_day_loss", "max_open_mints"})
            ok2, st2 = try_set_disabled(False)
            self.assertTrue(ok2)
            self.assertFalse(st2.disabled_switch)
            self.assertFalse(st2.armed)


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


class RiskGateLiveExtrasTests(unittest.TestCase):
    def test_live_extra_tags_registered(self):
        for tag in ("LIMITS_MISSING", "LIVE_DISABLED", "NO_KEYPAIR", "MAX_OPEN_MINTS"):
            self.assertIn(tag, REASON)

    def test_check_live_fail_closed_when_limits_missing(self):
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
            live_limits={},
        )
        self.assertFalse(risk.allow)
        self.assertIn("LIMITS_MISSING", risk.tags)

    def test_check_live_enforces_notional_and_open_mints(self):
        gate = RiskGate()
        ctx = StrategyContext(
            symbol="PUMPDEMO/SOL",
            ts=1,
            account=AccountCtx(day_pnl=0.0),
            liquidity=LiquidityCtx(spread_bps=10, adv_usd=1_000_000),
            meta={"open_mints": 2},
        )
        limits = {"max_notional_sol": 0.1, "max_day_loss": 1.0, "max_open_mints": 2}
        over = gate.check_live(
            ctx, SignalOut(side="long"), SizeIn(target_notional=0.5), live_limits=limits
        )
        self.assertFalse(over.allow)
        self.assertIn("MAX_NOTIONAL", over.tags)
        caps = gate.check_live(
            ctx, SignalOut(side="long"), SizeIn(target_notional=0.05), live_limits=limits
        )
        self.assertFalse(caps.allow)
        self.assertIn("MAX_OPEN_MINTS", caps.tags)

    def test_paper_check_ignores_unset_live_limits(self):
        gate = RiskGate()
        ctx = StrategyContext(
            symbol="PUMPDEMO/SOL",
            ts=1,
            account=AccountCtx(),
            liquidity=LiquidityCtx(spread_bps=10, adv_usd=1_000_000),
        )
        risk = gate.check(ctx, SignalOut(side="long"), SizeIn(target_notional=50))
        self.assertTrue(risk.allow)


class LiveRouteAndPaperTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["DATA_PROVIDER"] = "pumpfun_paper"
        os.environ["PUMPFUN_DISCOVERY"] = "off"
        os.environ["PUMP_PAPER_LOOP"] = "0"
        _clear_live_env()
        reset_live_state()
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
        os.environ["DATA_PROVIDER"] = "mock"
        reset_provider()
        reset_engine()
        reset_risk_gate()
        reset_paper_broker()
        reset_paper_ledger()

    def setUp(self):
        _clear_live_env()
        reset_live_state()
        reset_risk_gate()
        reset_paper_broker()

    def tearDown(self):
        _clear_live_env()
        reset_live_state()

    def test_health_live_disabled_default(self):
        r = self.client.get("/api/v1/health")
        self.assertEqual(r.status_code, 200)
        data = r.json()["data"]
        self.assertTrue(data["liveDisabled"])
        self.assertFalse(data["liveArmed"])
        self.assertFalse(data["liveSendWired"])
        self.assertIn(REASON_LIVE_DISABLED, data["liveReasons"])
        self.assertIn(REASON_NO_KEYPAIR, data["liveReasons"])
        self.assertIn(REASON_LIMITS_MISSING, data["liveReasons"])
        self.assertFalse(data["keypairConfigured"])

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
        self.assertIn(err["code"], {REASON_LIVE_DISABLED, REASON_NO_KEYPAIR, REASON_LIMITS_MISSING})
        self.assertIn(REASON_LIVE_DISABLED, err["reasons"])
        self.assertIn(REASON_NO_KEYPAIR, err["reasons"])
        self.assertIn(REASON_LIMITS_MISSING, err["reasons"])

    def test_arm_without_limits_403(self):
        r = self.client.put("/api/v1/live/arm", json={"armed": True})
        self.assertEqual(r.status_code, 403)
        self.assertIn(REASON_LIMITS_MISSING, r.json()["error"]["reasons"])

    def test_armed_stub_does_not_fill(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ[ENV_LIVE_DISABLED] = "false"
            os.environ[ENV_LIVE_ARMED] = "true"
            os.environ[ENV_KEYPAIR_PATH] = _dummy_keypair(tmp)
            os.environ[ENV_MAX_NOTIONAL_SOL] = "0.2"
            os.environ[ENV_MAX_DAY_LOSS] = "1"
            os.environ[ENV_MAX_OPEN_MINTS] = "3"
            reset_live_state()
            st = self.client.get("/api/v1/live/status").json()["data"]
            self.assertTrue(st["liveArmed"])
            self.assertTrue(st["liveDisabled"])
            self.assertFalse(st["sendEnabled"])
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

    def test_put_limits_empty_does_not_arm(self):
        r = self.client.put("/api/v1/live/limits", json={})
        self.assertEqual(r.status_code, 200)
        data = r.json()["data"]
        self.assertFalse(data["liveArmed"])
        self.assertIn(REASON_LIMITS_MISSING, data["reasons"])


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
        # Names split in paper/guard.py; live signer must also not concatenate them.
        needle = "PRIVATE" + "_KEY"
        hits: list[str] = []
        for path in LIVE_PY.rglob("*.py"):
            if needle in path.read_text(encoding="utf-8"):
                hits.append(str(path.relative_to(ROOT)))
        self.assertEqual(hits, [])


if __name__ == "__main__":
    unittest.main()
