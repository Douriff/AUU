"""Non-custodial wallet: pubkey bind, risk caps, unsigned devnet memo."""
from __future__ import annotations

import base64
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from nacl.signing import SigningKey

from app.live.gate import LOCKED_MAX_DAY_LOSS_PCT, LOCKED_MAX_NOTIONAL_SOL, LOCKED_MAX_OPEN_MINTS
from app.paper.ledger import get_paper_journal, reset_paper_ledger
from app.legacy.pump.wallet.codec import b58decode, b58encode, memo_program
from app.legacy.pump.wallet.service import (
    HARD_MAX_DAY_LOSS_PCT,
    HARD_MAX_NOTIONAL_SOL,
    HARD_MAX_OPEN_POSITIONS,
    reset_wallet,
)

ROOT = Path(__file__).resolve().parents[4]
WALLET_PY = ROOT / "apps" / "api" / "app" / "legacy" / "pump" / "wallet"


def _keypair():
    secret = SigningKey.generate()
    pubkey = b58encode(bytes(secret.verify_key))
    return secret, pubkey


def _sign_b64(secret: SigningKey, message: str) -> str:
    return base64.b64encode(secret.sign(message.encode("utf-8")).signature).decode("ascii")


def _sign_b58(secret: SigningKey, message: bytes) -> str:
    return b58encode(secret.sign(message).signature)


class WalletSourceTests(unittest.TestCase):
    def test_wallet_package_never_signs_or_imports_live(self):
        banned = ("SigningKey", "from app.live", "import app.live", "Keypair")
        hits: list[str] = []
        for path in WALLET_PY.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            for word in banned:
                if word in text:
                    hits.append(f"{path.name}:{word}")
            if "sendTransaction" in text and path.name != "orders.py":
                hits.append(f"{path.name}:sendTransaction")
        orders = WALLET_PY / "orders.py"
        if orders.exists() and orders.read_text(encoding="utf-8").count("sendTransaction") != 1:
            hits.append("orders.py:sendTransaction")
        self.assertEqual(hits, [])

    def test_hard_caps_match_live_limits_and_stay_paper(self):
        self.assertEqual(HARD_MAX_NOTIONAL_SOL, LOCKED_MAX_NOTIONAL_SOL)
        self.assertEqual(HARD_MAX_DAY_LOSS_PCT, LOCKED_MAX_DAY_LOSS_PCT)
        self.assertEqual(HARD_MAX_OPEN_POSITIONS, LOCKED_MAX_OPEN_MINTS)
        self.assertEqual(len(memo_program()), 32)


class WalletHttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self._prev = {key: os.environ.get(key) for key in (
            "AUU_AUTH",
            "AUU_ALLOW_SIGNUP",
            "AUU_INVITE_CODE",
            "AUU_ADMIN_USER",
            "AUU_USER_STORE",
            "AUU_USER_JOURNAL_DIR",
            "AUU_WALLET_MODE",
            "AUU_WALLET_STORE",
            "AUU_WALLET_CHALLENGE_TTL",
            "SOLANA_RPC_URL_DEVNET",
            "DATA_PROVIDER",
            "PUMP_PAPER_LOOP",
            "PUMPFUN_DISCOVERY",
            "PUMPFUN_WATCH_MINTS",
        )}
        os.environ["AUU_AUTH"] = "on"
        os.environ["AUU_ALLOW_SIGNUP"] = "on"
        os.environ.pop("AUU_INVITE_CODE", None)
        os.environ.pop("AUU_ADMIN_USER", None)
        os.environ["AUU_USER_STORE"] = str(root / "users.json")
        os.environ["AUU_USER_JOURNAL_DIR"] = str(root / "journals")
        os.environ["AUU_WALLET_MODE"] = "devnet"
        os.environ["AUU_WALLET_STORE"] = str(root / "wallets.json")
        os.environ["AUU_WALLET_CHALLENGE_TTL"] = "300"
        os.environ["SOLANA_RPC_URL_DEVNET"] = "https://rpc.invalid/devnet?api-key=sekret"
        os.environ["DATA_PROVIDER"] = "mock"
        os.environ["PUMP_PAPER_LOOP"] = "0"
        os.environ["PUMPFUN_DISCOVERY"] = "off"
        os.environ.pop("PUMPFUN_WATCH_MINTS", None)
        from app.auth.accounts import reset_accounts

        reset_accounts()
        reset_wallet()
        reset_paper_ledger()
        from fastapi.testclient import TestClient
        from app.main import app

        self.client = TestClient(app)
        self.other = TestClient(app)
        self.journal = get_paper_journal()
        self.fills_before = len(self.journal.fills)
        self._register(self.client, "alice")
        self._register(self.other, "bob")

    def tearDown(self):
        for key, value in self._prev.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        from app.auth.accounts import reset_accounts

        reset_accounts()
        reset_wallet()
        reset_paper_ledger()
        self.tmp.cleanup()

    def _register(self, client, name: str):
        res = client.post(
            "/api/v1/auth/register",
            json={"name": name, "password": "password1", "password_confirm": "password1"},
        )
        self.assertTrue(res.json()["ok"], res.text)
        return res.json()["data"]

    def _bind(self, client, secret: SigningKey, pubkey: str):
        challenge = client.post("/api/v1/wallet/challenge", json={"pubkey": pubkey})
        self.assertTrue(challenge.json()["ok"], challenge.text)
        body = challenge.json()["data"]
        self.assertIn("不授权转账", body["message"])
        self.assertIn(body["nonce"], body["message"])
        signed = client.post(
            "/api/v1/wallet/bind",
            json={
                "pubkey": pubkey,
                "nonce": body["nonce"],
                "signature": _sign_b64(secret, body["message"]),
            },
        )
        self.assertTrue(signed.json()["ok"], signed.text)
        self.assertFalse(signed.json()["data"]["liveEnabled"])
        self.assertEqual(signed.json()["data"]["pubkey"], pubkey)
        self.assertFalse(signed.json()["data"]["wallet_enabled"])
        return body

    def _rpc(self, method, params):
        if method == "getLatestBlockhash":
            return {"result": {"value": {"blockhash": b58encode(b"\x11" * 32)}}}
        if method == "getSignatureStatuses":
            return {"result": {"value": [{"confirmationStatus": "confirmed", "err": None}]}}
        raise AssertionError(method)

    def test_status_hides_rpc_and_live_stays_off(self):
        res = self.client.get("/api/v1/wallet/status")
        self.assertEqual(res.status_code, 200)
        text = res.text
        self.assertNotIn("sekret", text)
        self.assertNotIn("rpc.invalid", text)
        data = res.json()["data"]
        self.assertFalse(data["liveEnabled"])
        self.assertFalse(data["custodial"])
        self.assertEqual(data["wallet_mode"], "devnet")
        self.assertEqual(data["hard_limits"]["max_notional_sol"], 1.0)
        self.assertIn("不保存私钥", data["risk_text"])

    def test_bind_rejects_bad_expired_and_cross_user_signatures(self):
        secret, pubkey = _keypair()
        challenge = self.client.post("/api/v1/wallet/challenge", json={"pubkey": pubkey})
        body = challenge.json()["data"]
        bad = self.client.post(
            "/api/v1/wallet/bind",
            json={"pubkey": pubkey, "nonce": body["nonce"], "signature": base64.b64encode(b"\x01" * 64).decode()},
        )
        self.assertEqual(bad.json()["error"]["code"], "BAD_SIGNATURE")
        stolen = self.other.post(
            "/api/v1/wallet/bind",
            json={
                "pubkey": pubkey,
                "nonce": body["nonce"],
                "signature": _sign_b64(secret, body["message"]),
            },
        )
        self.assertEqual(stolen.json()["error"]["code"], "CHALLENGE")
        os.environ["AUU_WALLET_CHALLENGE_TTL"] = "-5"
        expired = self.client.post("/api/v1/wallet/challenge", json={"pubkey": pubkey})
        exp_body = expired.json()["data"]
        late = self.client.post(
            "/api/v1/wallet/bind",
            json={
                "pubkey": pubkey,
                "nonce": exp_body["nonce"],
                "signature": _sign_b64(secret, exp_body["message"]),
            },
        )
        self.assertEqual(late.json()["error"]["code"], "CHALLENGE")
        os.environ["AUU_WALLET_CHALLENGE_TTL"] = "300"
        self._bind(self.client, secret, pubkey)
        taken = self.other.post("/api/v1/wallet/challenge", json={"pubkey": pubkey})
        taken_body = taken.json()["data"]
        clash = self.other.post(
            "/api/v1/wallet/bind",
            json={
                "pubkey": pubkey,
                "nonce": taken_body["nonce"],
                "signature": _sign_b64(secret, taken_body["message"]),
            },
        )
        self.assertEqual(clash.json()["error"]["code"], "PUBKEY_TAKEN")

    def test_risk_can_only_get_stricter_and_mode_needs_consent(self):
        secret, pubkey = _keypair()
        loose = self.client.put(
            "/api/v1/wallet/risk",
            json={"max_notional_sol": 2, "max_day_loss_pct": 0.045, "max_open_positions": 10},
        )
        self.assertEqual(loose.json()["error"]["code"], "RISK_NOTIONAL")
        wider = self.client.put(
            "/api/v1/wallet/risk",
            json={"max_notional_sol": 1, "max_day_loss_pct": 0.05, "max_open_positions": 10},
        )
        self.assertEqual(wider.json()["error"]["code"], "RISK_DAY_LOSS")
        slots = self.client.put(
            "/api/v1/wallet/risk",
            json={"max_notional_sol": 1, "max_day_loss_pct": 0.045, "max_open_positions": 11},
        )
        self.assertEqual(slots.json()["error"]["code"], "RISK_POSITIONS")
        tight = self.client.put(
            "/api/v1/wallet/risk",
            json={"max_notional_sol": 0.2, "max_day_loss_pct": 0.045, "max_open_positions": 3},
        )
        self.assertTrue(tight.json()["ok"], tight.text)
        self.assertEqual(tight.json()["data"]["risk"]["max_open_positions"], 3)
        self._bind(self.client, secret, pubkey)
        denied = self.client.post("/api/v1/wallet/mode", json={"enabled": True, "accept_risk": False})
        self.assertEqual(denied.json()["error"]["code"], "RISK_CONSENT")
        enabled = self.client.post("/api/v1/wallet/mode", json={"enabled": True, "accept_risk": True})
        self.assertTrue(enabled.json()["data"]["wallet_enabled"])
        self.assertEqual(enabled.json()["data"]["consent"]["version"], "wallet-risk-v1")
        self.assertGreater(enabled.json()["data"]["consent"]["ts"], 0)

    def test_devnet_memo_is_unsigned_until_the_user_signs(self):
        secret, pubkey = _keypair()
        self._bind(self.client, secret, pubkey)
        self.client.post("/api/v1/wallet/mode", json={"enabled": True, "accept_risk": True})
        os.environ["AUU_WALLET_MODE"] = "off"
        blocked = self.client.post("/api/v1/wallet/devnet/prepare")
        self.assertEqual(blocked.json()["error"]["code"], "WALLET_OFF")
        os.environ["AUU_WALLET_MODE"] = "mainnet"
        mainnet = self.client.post("/api/v1/wallet/devnet/prepare")
        self.assertEqual(mainnet.json()["error"]["code"], "WALLET_DEVNET_ONLY")
        os.environ["AUU_WALLET_MODE"] = "devnet"
        with patch("app.legacy.pump.wallet.service.rpc", side_effect=self._rpc):
            prepared = self.client.post("/api/v1/wallet/devnet/prepare")
            self.assertTrue(prepared.json()["ok"], prepared.text)
            data = prepared.json()["data"]
            self.assertFalse(data["liveEnabled"])
            self.assertFalse(data["custodial"])
            self.assertEqual(data["amount_sol"], 0)
            raw = base64.b64decode(data["tx_base64"])
            self.assertEqual(raw[0], 1)
            self.assertEqual(raw[1:65], b"\x00" * 64)
            message = raw[65:]
            self.assertEqual(message[0:3], bytes([1, 0, 1]))
            self.assertEqual(message[4:36], b58decode(pubkey))
            forged = self.client.post(
                "/api/v1/wallet/devnet/record",
                json={"prepare_id": data["prepare_id"], "signature": b58encode(b"\x02" * 64)},
            )
            self.assertEqual(forged.json()["error"]["code"], "BAD_SIGNATURE")
            recorded = self.client.post(
                "/api/v1/wallet/devnet/record",
                json={"prepare_id": data["prepare_id"], "signature": _sign_b58(secret, message)},
            )
            self.assertTrue(recorded.json()["ok"], recorded.text)
            self.assertEqual(recorded.json()["data"]["item"]["status"], "confirmed")
            self.assertEqual(recorded.json()["data"]["item"]["network"], "devnet")
        mine = self.client.get("/api/v1/wallet/ledger")
        self.assertEqual(len(mine.json()["data"]["items"]), 1)
        self.assertNotIn("sekret", mine.text)
        theirs = self.other.get("/api/v1/wallet/ledger")
        self.assertEqual(theirs.json()["data"]["items"], [])
        cleared = self.client.post("/api/v1/wallet/unbind")
        self.assertIsNone(cleared.json()["data"]["pubkey"])
        self.assertFalse(cleared.json()["data"]["wallet_enabled"])
        still = self.client.get("/api/v1/wallet/ledger")
        self.assertEqual(len(still.json()["data"]["items"]), 1)
        self.assertIs(get_paper_journal(), self.journal)
        self.assertEqual(len(self.journal.fills), self.fills_before)

    def test_admin_halt_persists_and_does_not_opt_users_back_in(self):
        secret, pubkey = _keypair()
        self._bind(self.client, secret, pubkey)
        self.client.post("/api/v1/wallet/mode", json={"enabled": True, "accept_risk": True})
        forbidden = self.other.post("/api/v1/wallet/admin/halt", json={"halt": True})
        self.assertEqual(forbidden.status_code, 403)
        halted = self.client.post("/api/v1/wallet/admin/halt", json={"halt": True})
        self.assertTrue(halted.json()["data"]["global_halt"])
        self.assertFalse(halted.json()["data"]["liveEnabled"])
        reset_wallet()
        status = self.client.get("/api/v1/wallet/status")
        self.assertTrue(status.json()["data"]["global_halt"])
        self.assertFalse(status.json()["data"]["wallet_enabled"])
        again = self.client.post("/api/v1/wallet/mode", json={"enabled": True, "accept_risk": True})
        self.assertEqual(again.json()["error"]["code"], "WALLET_HALT")
        self.client.post("/api/v1/wallet/admin/halt", json={"halt": False})
        resumed = self.client.get("/api/v1/wallet/status")
        self.assertFalse(resumed.json()["data"]["global_halt"])
        self.assertFalse(resumed.json()["data"]["wallet_enabled"])
        prepared = self.client.post("/api/v1/wallet/devnet/prepare")
        self.assertEqual(prepared.json()["error"]["code"], "WALLET_MODE_OFF")

    def test_auth_off_keeps_wallet_bind_closed(self):
        os.environ["AUU_AUTH"] = "off"
        from app.auth.accounts import reset_accounts

        reset_accounts()
        guest = self.client.post("/api/v1/wallet/challenge", json={"pubkey": "x"})
        self.assertEqual(guest.status_code, 400)
        self.assertEqual(guest.json()["error"]["code"], "AUTH_OFF")
        status = self.client.get("/api/v1/wallet/status")
        self.assertFalse(status.json()["data"]["liveEnabled"])
        self.assertFalse(status.json()["data"]["bound"])


if __name__ == "__main__":
    unittest.main()
