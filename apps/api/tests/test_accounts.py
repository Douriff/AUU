"""Paper accounts stay isolated from each other and from the system journal."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from app.auth.accounts import journal_for, reset_accounts, user_by_id
from app.models.contracts import Fill
from app.paper.broker import reset_paper_broker
from app.paper.ledger import get_paper_journal, reset_paper_ledger
from app.providers import reset_provider
from app.risk.gate import get_risk_gate, reset_risk_gate
from app.strategies.pump_paper_v1 import reset_engine


class PaperAccountTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self._prev = {
            key: os.environ.get(key)
            for key in (
                "AUU_AUTH",
                "AUU_ALLOW_SIGNUP",
                "AUU_INVITE_CODE",
                "AUU_ADMIN_USER",
                "AUU_USER_STORE",
                "AUU_USER_JOURNAL_DIR",
                "AUU_PAPER_START_SOL",
                "DATA_PROVIDER",
                "PUMP_PAPER_LOOP",
                "PUMPFUN_DISCOVERY",
                "PUMPFUN_WATCH_MINTS",
            )
        }
        os.environ["AUU_AUTH"] = "on"
        os.environ["AUU_ALLOW_SIGNUP"] = "on"
        os.environ.pop("AUU_INVITE_CODE", None)
        os.environ.pop("AUU_ADMIN_USER", None)
        os.environ["AUU_USER_STORE"] = str(root / "users.json")
        os.environ["AUU_USER_JOURNAL_DIR"] = str(root / "journals")
        os.environ["DATA_PROVIDER"] = "pumpfun_paper"
        os.environ["PUMP_PAPER_LOOP"] = "0"
        os.environ["PUMPFUN_DISCOVERY"] = "off"
        os.environ.pop("PUMPFUN_WATCH_MINTS", None)
        reset_accounts()
        reset_provider()
        reset_engine()
        reset_risk_gate()
        reset_paper_broker()
        reset_paper_ledger()
        from fastapi.testclient import TestClient
        from app.main import app

        self.client = TestClient(app)
        self.other = TestClient(app)

    def tearDown(self):
        for key, value in self._prev.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        reset_accounts()
        reset_provider()
        reset_engine()
        reset_risk_gate()
        reset_paper_ledger()
        self.tmp.cleanup()

    def _strategy(self) -> dict:
        body = self.client.get("/api/v1/strategy/pump-paper-v1")
        self.assertEqual(body.status_code, 200)
        return body.json()["data"]

    def test_reader_has_no_live_path(self):
        root = Path("app/auth")
        text = "\n".join(path.read_text(encoding="utf-8") for path in root.rglob("*.py")).lower()
        self.assertNotIn("app.live", text)
        self.assertNotIn("sendtransaction", text)
        self.assertNotIn("withdraw", text)
        self.assertNotIn("deposit", text)

    def test_auth_off_keeps_local_trade_on_the_system_book(self):
        os.environ["AUU_AUTH"] = "off"
        reset_accounts()
        closed = self.client.post(
            "/api/v1/auth/register",
            json={"name": "local", "password": "password1"},
        )
        self.assertEqual(closed.status_code, 400)
        self.assertEqual(closed.json()["error"]["code"], "AUTH_OFF")
        board = self.client.get("/api/v1/leaderboard")
        self.assertEqual(board.status_code, 200)
        data = board.json()["data"]
        self.assertFalse(data["auth_enabled"])
        self.assertFalse(data["liveEnabled"])
        self.assertEqual(data["items"], [])
        self.assertIn("AUU_AUTH", data["note"])
        order = self.client.post(
            "/api/v1/trade/orders",
            json={"symbol": "PUMPDEMO/SOL", "side": "buy", "notional_sol": 0.1},
        )
        self.assertEqual(order.status_code, 200, order.text)
        self.assertTrue(order.json()["data"]["submitted"])
        self.assertIn("PUMPDEMO/SOL", get_paper_journal().lots)

    def test_two_users_do_not_share_fills_or_the_system_journal(self):
        before = self._strategy()
        day = float(get_risk_gate().day_pnl)
        alice = self.client.post(
            "/api/v1/auth/register",
            json={"name": "alice", "password": "password1", "start_sol": 80},
        )
        self.assertEqual(alice.status_code, 200, alice.text)
        self.assertTrue(alice.json()["data"]["user"]["is_admin"])
        self.assertFalse(alice.json()["data"]["liveEnabled"])
        bob = self.other.post(
            "/api/v1/auth/register",
            json={"name": "bob", "password": "password2", "start_sol": 50},
        )
        self.assertEqual(bob.status_code, 200, bob.text)
        self.assertFalse(bob.json()["data"]["user"]["is_admin"])

        from fastapi.testclient import TestClient
        from app.main import app

        guest = TestClient(app)
        denied = guest.post(
            "/api/v1/trade/orders",
            json={"symbol": "PUMPDEMO/SOL", "side": "buy", "notional_sol": 0.1},
        )
        self.assertEqual(denied.status_code, 401)

        buy = self.client.post(
            "/api/v1/trade/orders",
            json={"symbol": "PUMPDEMO/SOL", "side": "buy", "notional_sol": 0.2},
        )
        self.assertEqual(buy.status_code, 200, buy.text)
        self.assertTrue(buy.json()["data"]["submitted"])
        self.assertFalse(buy.json()["data"]["liveEnabled"])
        self.assertGreater(self.client.get("/api/v1/trade/position", params={"symbol": "PUMPDEMO/SOL"}).json()["data"]["qty"], 0)
        bob_pos = self.other.get("/api/v1/trade/position", params={"symbol": "PUMPDEMO/SOL"})
        self.assertEqual(bob_pos.status_code, 200)
        self.assertEqual(bob_pos.json()["data"]["qty"], 0)
        self.assertFalse(get_paper_journal().fills)
        self.assertFalse(get_paper_journal().lots)
        self.assertEqual(get_paper_journal().closed, [])
        self.assertEqual(float(get_risk_gate().day_pnl), day)
        after = self._strategy()
        self.assertEqual(after["params"], before["params"])

        cap = self.client.post(
            "/api/v1/trade/orders",
            json={"symbol": "PUMPDEMO/SOL", "side": "buy", "notional_sol": 1.5},
        )
        self.assertEqual(cap.status_code, 400)
        self.assertEqual(cap.json()["error"]["code"], "MAX_NOTIONAL")

        users = self.client.get("/api/v1/auth/users")
        self.assertEqual(users.status_code, 200)
        self.assertEqual({row["name"] for row in users.json()["data"]["items"]}, {"alice", "bob"})
        self.assertFalse(self.other.get("/api/v1/auth/users").status_code == 200)
        self.assertEqual(self.other.get("/api/v1/auth/users").status_code, 403)
        alice_id = alice.json()["data"]["user"]["id"]
        bob_id = bob.json()["data"]["user"]["id"]
        peeked = self.client.get("/api/v1/trade/book", params={"user_id": bob_id})
        self.assertEqual(peeked.status_code, 200)
        self.assertEqual(peeked.json()["data"]["user"]["name"], "bob")
        self.assertEqual(peeked.json()["data"]["items"], [])
        hidden = self.other.get("/api/v1/trade/book", params={"user_id": alice_id})
        self.assertEqual(hidden.status_code, 403)

        book = journal_for(user_by_id(alice_id))
        book.record_fill(
            "PUMPDEMO/SOL",
            Fill(ts=2_000, price=2.0, qty=-abs(book.lots["PUMPDEMO/SOL"][0].qty), fee=0.0, tag="paper:manual:source=manual"),
            announce=False,
        )
        board = self.client.get("/api/v1/leaderboard").json()["data"]
        self.assertFalse(board["liveEnabled"])
        self.assertEqual(board["items"][0]["name"], "alice")
        self.assertGreater(board["items"][0]["pnl"], board["items"][1]["pnl"])
        self.assertEqual(get_paper_journal().closed, [])

        stored = json.loads(Path(os.environ["AUU_USER_STORE"]).read_text(encoding="utf-8"))
        blob = json.dumps(stored)
        self.assertNotIn("password1", blob)
        self.assertNotIn("password2", blob)
        self.assertTrue(stored["users"][0]["password_hash"].startswith("$2"))

        self.client.post("/api/v1/auth/logout")
        again = self.client.post(
            "/api/v1/trade/orders",
            json={"symbol": "PUMPDEMO/SOL", "side": "buy", "notional_sol": 0.1},
        )
        self.assertEqual(again.status_code, 401)

    def test_signup_invite_and_password_rules(self):
        os.environ["AUU_ALLOW_SIGNUP"] = "off"
        closed = self.client.post(
            "/api/v1/auth/register",
            json={"name": "nina", "password": "password1"},
        )
        self.assertEqual(closed.status_code, 400)
        self.assertEqual(closed.json()["error"]["code"], "SIGNUP_CLOSED")
        os.environ["AUU_ALLOW_SIGNUP"] = "on"
        os.environ["AUU_INVITE_CODE"] = "letmein"
        bad = self.client.post(
            "/api/v1/auth/register",
            json={"name": "nina", "password": "password1", "invite": "nope"},
        )
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(bad.json()["error"]["code"], "INVITE")
        short = self.client.post(
            "/api/v1/auth/register",
            json={"name": "nina", "password": "short", "invite": "letmein"},
        )
        self.assertEqual(short.status_code, 400)
        self.assertEqual(short.json()["error"]["code"], "BAD_PASSWORD")
        ok = self.client.post(
            "/api/v1/auth/register",
            json={"name": "nina", "password": "password1", "invite": "letmein"},
        )
        self.assertEqual(ok.status_code, 200, ok.text)
        self.assertEqual(ok.json()["data"]["user"]["name"], "nina")
        me = self.client.get("/api/v1/auth/me")
        self.assertEqual(me.json()["data"]["user"]["name"], "nina")
        self.assertFalse(me.json()["data"]["liveEnabled"])


if __name__ == "__main__":
    unittest.main()
