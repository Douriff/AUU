"""Mainnet wallet orders stay unsigned until the user signs, and tests never broadcast."""
from __future__ import annotations

import base64
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from nacl.signing import SigningKey

from app.paper.ledger import get_paper_journal, reset_paper_ledger
from app.legacy.pump.strategies.pump_paper_v1 import PumpPaperParams, current_params
from app.legacy.pump.wallet.codec import b58decode, b58encode, build_memo_message, split_transaction, unsigned_transaction
from app.legacy.pump.wallet.service import reset_wallet


def _keypair():
    secret = SigningKey.generate()
    return secret, b58encode(bytes(secret.verify_key))


def _unsigned(pubkey: str, memo: str = "AUU mainnet order") -> bytes:
    message = build_memo_message(b58decode(pubkey), b"\x11" * 32, memo)
    return unsigned_transaction(message)


def _signed(secret: SigningKey, raw: bytes, message: bytes | None = None) -> str:
    _sigs, stored = split_transaction(raw)
    body = stored if message is None else message
    signature = secret.sign(body).signature
    packed = bytes([raw[0]]) + signature + body
    return base64.b64encode(packed).decode("ascii")


class WalletOrderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        keys = (
            "AUU_AUTH",
            "AUU_ALLOW_SIGNUP",
            "AUU_INVITE_CODE",
            "AUU_ADMIN_USER",
            "AUU_USER_STORE",
            "AUU_USER_JOURNAL_DIR",
            "AUU_WALLET_MODE",
            "AUU_WALLET_STORE",
            "AUU_WALLET_CHALLENGE_TTL",
            "AUU_WALLET_PREPARE_TTL",
            "SOLANA_RPC_URL_MAINNET",
            "PUMPPORTAL_TRADE_LOCAL_URL",
            "DATA_PROVIDER",
            "PUMP_PAPER_LOOP",
            "PUMPFUN_DISCOVERY",
            "PUMPFUN_WATCH_MINTS",
        )
        self._prev = {key: os.environ.get(key) for key in keys}
        os.environ["AUU_AUTH"] = "on"
        os.environ["AUU_ALLOW_SIGNUP"] = "on"
        os.environ.pop("AUU_INVITE_CODE", None)
        os.environ.pop("AUU_ADMIN_USER", None)
        os.environ["AUU_USER_STORE"] = str(root / "users.json")
        os.environ["AUU_USER_JOURNAL_DIR"] = str(root / "journals")
        os.environ["AUU_WALLET_MODE"] = "mainnet"
        os.environ["AUU_WALLET_STORE"] = str(root / "wallets.json")
        os.environ["AUU_WALLET_CHALLENGE_TTL"] = "300"
        os.environ["AUU_WALLET_PREPARE_TTL"] = "90"
        os.environ["SOLANA_RPC_URL_MAINNET"] = "https://rpc.invalid/mainnet?api-key=sekret"
        os.environ["PUMPPORTAL_TRADE_LOCAL_URL"] = "https://portal.invalid/trade-local?api-key=sekret"
        os.environ["DATA_PROVIDER"] = "mock"
        os.environ["PUMP_PAPER_LOOP"] = "0"
        os.environ["PUMPFUN_DISCOVERY"] = "off"
        os.environ.pop("PUMPFUN_WATCH_MINTS", None)
        from app.auth.accounts import reset_accounts

        reset_accounts()
        reset_wallet()
        reset_paper_ledger()
        self.params_before = current_params()[0].model_dump()
        from fastapi.testclient import TestClient
        from app.main import app

        self.client = TestClient(app)
        self.other = TestClient(app)
        self.journal = get_paper_journal()
        self.fills_before = len(self.journal.fills)
        self.client.post(
            "/api/v1/auth/register",
            json={"name": "mina", "password": "password1", "password_confirm": "password1"},
        )
        self.other.post(
            "/api/v1/auth/register",
            json={"name": "bob", "password": "password1", "password_confirm": "password1"},
        )
        self.secret, self.pubkey = _keypair()
        self.mint_secret, self.mint = _keypair()
        self.portal_calls: list[dict] = []
        self._bind(self.client, self.secret, self.pubkey)
        self.client.post("/api/v1/wallet/mode", json={"enabled": True, "accept_risk": True})

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

    def _bind(self, client, secret: SigningKey, pubkey: str):
        challenge = client.post("/api/v1/wallet/challenge", json={"pubkey": pubkey})
        body = challenge.json()["data"]
        signed = client.post(
            "/api/v1/wallet/bind",
            json={
                "pubkey": pubkey,
                "nonce": body["nonce"],
                "signature": base64.b64encode(secret.sign(body["message"].encode()).signature).decode(),
            },
        )
        self.assertTrue(signed.json()["ok"], signed.text)

    def _fetch(self, payload):
        self.portal_calls.append(payload)
        return _unsigned(payload["publicKey"])

    def _rpc(self, method, params, url=None):
        if method == "sendTransaction":
            raw = base64.b64decode(params[0])
            signatures, _message = split_transaction(raw)
            return {"result": b58encode(signatures[0])}
        if method == "getSignatureStatuses":
            return {"result": {"value": [None]}}
        raise AssertionError(method)

    def _prepare(self, fetch=None, **extra):
        body = {"mint": self.mint, "side": "buy", "notional_sol": 0.1, "price_sol": 0.001}
        body.update(extra)
        with patch("app.legacy.pump.wallet.orders.fetch_trade_local", side_effect=fetch or self._fetch):
            return self.client.post("/api/v1/wallet/order/prepare", json=body)

    def test_signal_reuses_strategy_levels_and_caps_size(self):
        res = self.client.get("/api/v1/wallet/signal", params={"mint": self.mint, "price_sol": 0.001})
        self.assertTrue(res.json()["ok"], res.text)
        data = res.json()["data"]
        self.assertFalse(data["liveEnabled"])
        self.assertEqual(data["strategy_id"], "pump-paper-v1")
        self.assertAlmostEqual(data["entry_price"], 0.001)
        self.assertAlmostEqual(data["take_profit"], 0.001 * 1.06)
        self.assertAlmostEqual(data["stop_loss"], 0.001 * 0.95)
        self.assertAlmostEqual(data["suggested_sol"], 0.12)
        self.assertEqual(data["slippage_bps"], 150)
        self.assertTrue(data["real_money"])
        self.assertTrue(data["order_allowed"])
        self.assertNotIn("sekret", res.text)
        tight = self.client.put(
            "/api/v1/wallet/risk",
            json={"max_notional_sol": 0.05, "max_day_loss_pct": 0.02, "max_open_positions": 2},
        )
        self.assertTrue(tight.json()["ok"], tight.text)
        capped = self.client.get("/api/v1/wallet/signal", params={"price_sol": 0.001})
        self.assertAlmostEqual(capped.json()["data"]["suggested_sol"], 0.05)
        self.assertEqual(current_params()[0].model_dump(), self.params_before)
        fresh = PumpPaperParams()
        self.assertEqual(fresh.take_profit_pct, 0.06)
        self.assertEqual(fresh.stop_loss_pct, 0.05)
        self.assertEqual(fresh.max_notional_sol, 0.12)
        self.assertFalse(fresh.auto_paper_orders)

    def test_prepare_rejects_loose_risk_without_calling_portal(self):
        wide = self._prepare(slippage_bps=200)
        fat = self._prepare(notional_sol=2)
        self.assertEqual(wide.json()["error"]["code"], "SLIPPAGE")
        self.assertEqual(fat.json()["error"]["code"], "RISK_NOTIONAL")
        self.assertEqual(self.portal_calls, [])

    def test_submit_checks_signature_message_and_expiry(self):
        os.environ["AUU_WALLET_MODE"] = "off"
        closed = self._prepare()
        self.assertEqual(closed.json()["error"]["code"], "WALLET_OFF")
        os.environ["AUU_WALLET_MODE"] = "devnet"
        devnet = self._prepare()
        self.assertEqual(devnet.json()["error"]["code"], "WALLET_MAINNET_ONLY")
        os.environ["AUU_WALLET_MODE"] = "mainnet"
        prepared = self._prepare()
        self.assertTrue(prepared.json()["ok"], prepared.text)
        data = prepared.json()["data"]
        self.assertFalse(data["liveEnabled"])
        self.assertTrue(data["real_money"])
        self.assertGreater(data["expires_at"], 0)
        self.assertEqual(self.portal_calls[-1]["publicKey"], self.pubkey)
        self.assertEqual(self.portal_calls[-1]["action"], "buy")
        self.assertAlmostEqual(self.portal_calls[-1]["slippage"], 1.5)
        self.assertNotIn("sekret", prepared.text)
        raw = base64.b64decode(data["tx_base64"])
        _sigs, message = split_transaction(raw)
        self.assertEqual(message[4:36], b58decode(self.pubkey))
        flipped = bytearray(message)
        flipped[-1] ^= 0x01
        tampered = self.client.post(
            "/api/v1/wallet/order/submit",
            json={"prepare_id": data["prepare_id"], "signed_tx": _signed(self.secret, raw, bytes(flipped))},
        )
        self.assertEqual(tampered.json()["error"]["code"], "TAMPER")
        other, _other_pub = _keypair()
        stolen = self.client.post(
            "/api/v1/wallet/order/submit",
            json={"prepare_id": data["prepare_id"], "signed_tx": _signed(other, raw)},
        )
        self.assertEqual(stolen.json()["error"]["code"], "BAD_SIGNATURE")
        with patch("app.legacy.pump.wallet.orders.rpc", side_effect=self._rpc), patch("app.legacy.pump.wallet.service.rpc", side_effect=self._rpc):
            done = self.client.post(
                "/api/v1/wallet/order/submit",
                json={"prepare_id": data["prepare_id"], "signed_tx": _signed(self.secret, raw)},
            )
            self.assertTrue(done.json()["ok"], done.text)
            self.assertEqual(done.json()["data"]["item"]["status"], "submitted")
            self.assertEqual(done.json()["data"]["item"]["network"], "mainnet")
            self.assertNotIn("sekret", done.text)
            book = self.client.get("/api/v1/wallet/ledger")
        self.assertEqual(len(book.json()["data"]["items"]), 1)
        self.assertEqual(self.other.get("/api/v1/wallet/ledger").json()["data"]["items"], [])
        os.environ["AUU_WALLET_PREPARE_TTL"] = "-5"
        expired = self._prepare()
        late = self.client.post(
            "/api/v1/wallet/order/submit",
            json={
                "prepare_id": expired.json()["data"]["prepare_id"],
                "signed_tx": _signed(self.secret, base64.b64decode(expired.json()["data"]["tx_base64"])),
            },
        )
        self.assertEqual(late.json()["error"]["code"], "PREPARE")
        self.assertIs(get_paper_journal(), self.journal)
        self.assertEqual(len(self.journal.fills), self.fills_before)
        self.assertEqual(current_params()[0].model_dump(), self.params_before)

    def test_day_loss_blocks_buys_and_sells_still_need_a_signature(self):
        with patch("app.legacy.pump.wallet.orders.rpc", side_effect=self._rpc), patch("app.legacy.pump.wallet.service.rpc", side_effect=self._rpc):
            prepared = self._prepare()
            raw = base64.b64decode(prepared.json()["data"]["tx_base64"])
            self.client.post(
                "/api/v1/wallet/order/submit",
                json={"prepare_id": prepared.json()["data"]["prepare_id"], "signed_tx": _signed(self.secret, raw)},
            )
            sell = self._prepare(side="sell", sell_pct=100, price_sol=0.0005)
            self.assertTrue(sell.json()["ok"], sell.text)
            sell_raw = base64.b64decode(sell.json()["data"]["tx_base64"])
            closed = self.client.post(
                "/api/v1/wallet/order/submit",
                json={"prepare_id": sell.json()["data"]["prepare_id"], "signed_tx": _signed(self.secret, sell_raw)},
            )
            self.assertTrue(closed.json()["ok"], closed.text)
            self.assertLess(closed.json()["data"]["item"]["pnl_sol"], 0)
            blocked = self._prepare()
        self.assertEqual(blocked.json()["error"]["code"], "DAY_LOSS")
        self.assertEqual(self.portal_calls[-1]["action"], "sell")

    def test_portal_fee_payer_must_be_the_bound_pubkey(self):
        _other, other_pub = _keypair()

        def wrong(_payload):
            return _unsigned(other_pub)

        res = self._prepare(fetch=wrong)
        self.assertEqual(res.json()["error"]["code"], "TAMPER")


if __name__ == "__main__":
    unittest.main()
