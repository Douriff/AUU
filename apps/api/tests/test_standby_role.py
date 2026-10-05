"""AUU_ROLE=standby: a DR node serves the site but never writes ledgers or sends mail."""
from __future__ import annotations

import os
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import role

# Per-job switches that would otherwise be "on" by default.
_JOB_ENV = {"AUU_STRATEGY_RUNNER": "on", "AUU_SHADOW_H2": "on", "AUU_SHADOW_S3": "on", "AUU_ALERTS": "on",
            "AUU_RECON": "on", "AUU_UPTIME": "on", "AUU_EXEC_SHADOW": "on"}


def _enabled() -> dict:
    from app.alerts import alerts_enabled
    from app.marketdata.mainstream.recon import recon_enabled
    from app.paper.shadow_h2 import shadow_enabled as h2_enabled
    from app.paper.shadow_s3 import shadow_enabled as s3_enabled
    from app.paper.strategy_runner import _exec_shadow_hook, runner_enabled
    from app.status import uptime_enabled

    return {"strategy": runner_enabled(), "exec_shadow": _exec_shadow_hook() is not None, "shadow_s3": s3_enabled(),
            "shadow_h2": h2_enabled(), "alerts": alerts_enabled(), "recon": recon_enabled(), "uptime": uptime_enabled()}


class RoleParseTests(unittest.TestCase):
    def test_default_and_primary(self):
        for raw in (None, "", "primary", " PRIMARY "):
            env = {} if raw is None else {"AUU_ROLE": raw}
            with patch.dict(os.environ, env, clear=False):
                if raw is None:
                    os.environ.pop("AUU_ROLE", None)
                self.assertEqual(role.node_role(), "primary", raw)
                self.assertFalse(role.is_standby())

    def test_standby_and_fail_safe(self):
        for raw in ("standby", "Standby", "stanby", "backup", "hk"):
            with patch.dict(os.environ, {"AUU_ROLE": raw}):
                self.assertEqual(role.node_role(), "standby", raw)
                self.assertTrue(role.is_standby())


class StandbyJobTests(unittest.TestCase):
    def test_primary_keeps_every_job(self):
        with patch.dict(os.environ, {**_JOB_ENV, "AUU_ROLE": "primary"}):
            self.assertEqual(set(k for k, v in _enabled().items() if v), set(role.STANDBY_OFF))

    def test_standby_turns_every_writer_and_mailer_off(self):
        with patch.dict(os.environ, {**_JOB_ENV, "AUU_ROLE": "standby"}):
            self.assertEqual(_enabled(), {k: False for k in role.STANDBY_OFF})
            self.assertEqual(role.role_status(), {"role": "standby", "standby": True, "jobsOff": list(role.STANDBY_OFF)})

    def test_standby_overrides_explicit_on(self):
        with patch.dict(os.environ, {**_JOB_ENV, "AUU_ROLE": "standby", "AUU_STRATEGY_RUNNER": "1", "AUU_ALERTS": "true"}):
            self.assertFalse(any(_enabled().values()))

    def test_write_block_rules(self):
        with patch.dict(os.environ, {"AUU_ROLE": "standby"}):
            self.assertTrue(role.standby_blocks("POST", "/api/v1/mainstream/paper/orders"))
            self.assertTrue(role.standby_blocks("delete", "/api/v1/mainstream/paper/orders/x"))
            self.assertTrue(role.standby_blocks("POST", "/api/v1/live/enabled"))
            self.assertFalse(role.standby_blocks("GET", "/api/v1/mainstream/strategy"))
            self.assertFalse(role.standby_blocks("POST", "/api/v1/auth/login"))
        with patch.dict(os.environ, {"AUU_ROLE": "primary"}):
            self.assertFalse(role.standby_blocks("POST", "/api/v1/mainstream/paper/orders"))


class StandbyAppTests(unittest.TestCase):
    """The lifespan starts no ledger/mail task on a standby; health reports the role; writes get 503."""

    _ENV = ("AUU_AUTH", "AUU_ALLOW_SIGNUP", "AUU_USER_STORE", "AUU_USER_JOURNAL_DIR", "AUU_AUTH_RATE_MAX", "AUU_LEGACY_PUMP",
            "AUU_INVITE_CODE", "AUU_ADMIN_USER", "AUU_ROLE", "AUU_MAINSTREAM_REFRESH", "AUU_NEWS", *_JOB_ENV)

    def setUp(self):
        from app.auth.accounts import reset_accounts

        self.tmp = tempfile.TemporaryDirectory()
        self._prev = {k: os.environ.get(k) for k in self._ENV}
        root = Path(self.tmp.name)
        os.environ.update({"AUU_AUTH": "on", "AUU_ALLOW_SIGNUP": "on", "AUU_USER_STORE": str(root / "users.json"),
                           "AUU_USER_JOURNAL_DIR": str(root / "journals"), "AUU_AUTH_RATE_MAX": "1000",
                           "AUU_LEGACY_PUMP": "off", "AUU_ROLE": "standby", "AUU_MAINSTREAM_REFRESH": "off",
                           "AUU_NEWS": "off", **_JOB_ENV})
        os.environ.pop("AUU_INVITE_CODE", None)
        os.environ.pop("AUU_ADMIN_USER", None)
        reset_accounts()
        self._sock = patch.object(socket.socket, "connect", side_effect=AssertionError("network access in test"))
        self._sock.start()

    def tearDown(self):
        from app.auth.accounts import reset_accounts

        self._sock.stop()
        for k, v in self._prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        reset_accounts()
        self.tmp.cleanup()

    def test_lifespan_health_and_write_block(self):
        import asyncio

        from fastapi.testclient import TestClient

        from app.main import create_app

        started: list[str] = []
        real = asyncio.create_task

        def spy(coro, *a, **kw):
            started.append(kw.get("name") or "")
            return real(coro, *a, **kw)

        with patch("asyncio.create_task", side_effect=spy):
            with TestClient(create_app(legacy=False)) as c:
                h = c.get("/api/v1/health").json()["data"]
                self.assertEqual(h["role"], "standby")
                self.assertTrue(h["standby"]["standby"])
                self.assertFalse(h["strategyRunner"]["active"])
                self.assertEqual(h["strategyRunner"]["reason"], "standby")
                self.assertFalse(h["strategyRunner"]["stalled"])
                self.assertFalse(h["alerts"]["enabled"])
                self.assertFalse(h["liveEnabled"])
                r = c.post("/api/v1/mainstream/paper/orders", json={})
                self.assertEqual(r.status_code, 503)
                self.assertEqual(r.json()["error"]["code"], "STANDBY")
                pw = "standbyPass123"
                self.assertEqual(c.post("/api/v1/auth/register", json={"name": "sb", "password": pw,
                                                                        "password_confirm": pw}).status_code, 200)
        for name in ("mainstream-strategy", "shadow-s3", "shadow-h2", "alerts", "recon", "uptime"):
            self.assertNotIn(name, started)

    def test_primary_starts_jobs_and_reports_primary(self):
        import asyncio

        from fastapi.testclient import TestClient

        from app.main import create_app

        os.environ["AUU_ROLE"] = "primary"
        started: list[str] = []
        real = asyncio.create_task

        def spy(coro, *a, **kw):
            started.append(kw.get("name") or "")
            return real(coro, *a, **kw)

        with patch("asyncio.create_task", side_effect=spy):
            with TestClient(create_app(legacy=False)) as c:
                h = c.get("/api/v1/health").json()["data"]
                self.assertEqual(h["role"], "primary")
                self.assertFalse(h["standby"]["standby"])
        # same spy sees the primary's jobs, so the standby assertion above is meaningful
        for name in ("mainstream-strategy", "shadow-h2", "alerts"):
            self.assertIn(name, started)


if __name__ == "__main__":
    unittest.main()
