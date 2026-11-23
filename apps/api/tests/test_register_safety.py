"""Public signup must fail closed on privilege, store faults, and concurrent duplicates.

These are the official regressions for the independent-review HTTP counterexamples.
They use the existing user store only. No second database, no real SMTP, no live trading.
"""
from __future__ import annotations

import concurrent.futures
import json
import os
import tempfile
import threading
import time
import unittest
from multiprocessing import get_context
from pathlib import Path
from unittest import mock

from app.auth.accounts import StoreUnavailable, bootstrap_admin, reset_accounts
from tests.auth_fixtures import seed_admin


_KEYS = (
    "AUU_AUTH",
    "AUU_ALLOW_SIGNUP",
    "AUU_USER_STORE",
    "AUU_USER_JOURNAL_DIR",
    "AUU_AUTH_RATE_MAX",
    "AUU_INVITE_CODE",
    "AUU_ADMIN_USER",
    "AUU_EMAIL_VERIFY",
    "DATA_PROVIDER",
    "PUMP_PAPER_LOOP",
    "PUMPFUN_DISCOVERY",
)


def _child_register(env: dict[str, str], name: str, out: str) -> None:
    for key, value in env.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    os.environ.pop("AUU_INVITE_CODE", None)
    from app.auth.accounts import register, reset_accounts

    reset_accounts()
    try:
        user = register(name, "password1")
        Path(out).write_text(json.dumps({"ok": True, "id": user["id"], "is_admin": user["is_admin"], "name": user["name"]}), encoding="utf-8")
    except Exception as exc:
        Path(out).write_text(json.dumps({"ok": False, "code": str(exc)}), encoding="utf-8")


class RegisterSafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.store = root / "users.json"
        self._prev = {key: os.environ.get(key) for key in _KEYS}
        os.environ["AUU_AUTH"] = "on"
        os.environ["AUU_ALLOW_SIGNUP"] = "on"
        os.environ["AUU_AUTH_RATE_MAX"] = "1000"
        os.environ["AUU_USER_STORE"] = str(self.store)
        os.environ["AUU_USER_JOURNAL_DIR"] = str(root / "journals")
        os.environ["AUU_ADMIN_USER"] = "reservedowner"
        os.environ["AUU_EMAIL_VERIFY"] = "off"
        os.environ["DATA_PROVIDER"] = "pumpfun_paper"
        os.environ["PUMP_PAPER_LOOP"] = "0"
        os.environ["PUMPFUN_DISCOVERY"] = "off"
        os.environ.pop("AUU_INVITE_CODE", None)
        reset_accounts()
        from fastapi.testclient import TestClient
        from app.main import app

        self.app = app
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        reset_accounts()
        for key, value in self._prev.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp.cleanup()

    def _users(self) -> list[dict]:
        if not self.store.exists():
            return []
        return json.loads(self.store.read_text(encoding="utf-8"))["users"]

    def test_empty_store_public_register_fails_closed(self):
        me = self.client.get("/api/v1/auth/me").json()["data"]
        self.assertFalse(me["signup_allowed"])
        res = self.client.post(
            "/api/v1/auth/register",
            json={"name": "firstpublic", "password": "password1", "password_confirm": "password1"},
        )
        self.assertEqual(res.status_code, 400, res.text)
        self.assertEqual(res.json()["error"]["code"], "SIGNUP_NOT_READY")
        self.assertFalse(self._users())
        self.assertIsNone(self.client.get("/api/v1/auth/me").json()["data"]["user"])

    def test_missing_store_public_register_does_not_create_admin(self):
        self.assertFalse(self.store.exists())
        res = self.client.post(
            "/api/v1/auth/register",
            json={"name": "reservedowner", "password": "password1", "password_confirm": "password1"},
        )
        self.assertEqual(res.status_code, 400, res.text)
        self.assertEqual(res.json()["error"]["code"], "SIGNUP_NOT_READY")
        self.assertFalse(self.store.exists() and self._users())

    def test_reserved_name_cannot_be_claimed_in_nonempty_store(self):
        seed_admin("bootstrapowner", "password1")
        reset_accounts()
        me = self.client.get("/api/v1/auth/me").json()["data"]
        self.assertTrue(me["signup_allowed"])
        claimed = self.client.post(
            "/api/v1/auth/register",
            json={"name": "reservedowner", "password": "password1", "password_confirm": "password1"},
        )
        self.assertEqual(claimed.status_code, 400, claimed.text)
        self.assertEqual(claimed.json()["error"]["code"], "NAME_TAKEN")
        self.assertIsNone(self.client.get("/api/v1/auth/me").json()["data"]["user"])
        names = {row["name"]: row for row in self._users()}
        self.assertNotIn("reservedowner", names)
        self.assertTrue(names["bootstrapowner"]["is_admin"])
        # case-insensitive reserved name
        mixed = self.client.post(
            "/api/v1/auth/register",
            json={"name": "ReservedOwner", "password": "password1", "password_confirm": "password1"},
        )
        self.assertEqual(mixed.status_code, 400)
        self.assertEqual(mixed.json()["error"]["code"], "NAME_TAKEN")
        self.assertEqual({row["name"] for row in self._users()}, {"bootstrapowner"})

    def test_public_signup_never_sets_admin_after_bootstrap(self):
        seed_admin("bootstrapowner", "password1")
        created = self.client.post(
            "/api/v1/auth/register",
            json={"name": "paperuser", "password": "password1", "password_confirm": "password1"},
        )
        self.assertEqual(created.status_code, 200, created.text)
        self.assertFalse(created.json()["data"]["user"]["is_admin"])
        self.assertFalse(created.json()["data"]["liveEnabled"])

    def test_corrupt_store_fails_closed_and_is_not_rewritten(self):
        payload = "{not-json"
        self.store.write_text(payload, encoding="utf-8")
        before = self.store.read_bytes()
        reset_accounts()
        me = self.client.get("/api/v1/auth/me")
        self.assertEqual(me.status_code, 200)
        self.assertFalse(me.json()["data"]["signup_allowed"])
        res = self.client.post(
            "/api/v1/auth/register",
            json={"name": "paperuser", "password": "password1", "password_confirm": "password1"},
        )
        self.assertIn(res.status_code, (400, 503), res.text)
        self.assertEqual(res.json()["error"]["code"], "STORE_UNAVAILABLE")
        self.assertEqual(self.store.read_bytes(), before)
        with self.assertRaises(StoreUnavailable):
            bootstrap_admin("owner", "password1")
        self.assertEqual(self.store.read_bytes(), before)

    def test_bootstrap_creates_first_admin_and_refuses_a_second(self):
        first = seed_admin("owner", "password1")
        self.assertTrue(first["is_admin"])
        self.assertEqual(first["name"], "owner")
        with self.assertRaises(ValueError) as caught:
            bootstrap_admin("other", "password1")
        self.assertEqual(str(caught.exception), "ADMIN_EXISTS")
        rows = self._users()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["name"], "owner")
        self.assertTrue(rows[0]["is_admin"])
        # Existing admin is unchanged; public user can be added without promotion.
        created = self.client.post(
            "/api/v1/auth/register",
            json={"name": "member", "password": "password1", "password_confirm": "password1"},
        )
        self.assertEqual(created.status_code, 200)
        self.assertFalse(created.json()["data"]["user"]["is_admin"])
        names = {row["name"]: row["is_admin"] for row in self._users()}
        self.assertEqual(names, {"owner": True, "member": False})

    def test_http_simultaneous_normalized_duplicate_names_keep_one_row(self):
        seed_admin("bootstrapowner", "password1")
        import app.auth.accounts as accounts

        barrier = threading.Barrier(2)
        original = accounts.hash_password

        def concurrent_hash(password):
            barrier.wait(timeout=10)
            return original(password)

        def signup(name: str):
            client = self.client.__class__(self.app)
            try:
                res = client.post(
                    "/api/v1/auth/register",
                    json={"name": name, "password": "password1", "password_confirm": "password1"},
                )
                return res.status_code, res.json()
            finally:
                client.close()

        with mock.patch.object(accounts, "hash_password", concurrent_hash):
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                rows = list(pool.map(signup, ["raceruser", "RACERUSER"]))
        statuses = sorted(r[0] for r in rows)
        self.assertEqual(statuses, [200, 400], rows)
        taken = [r for r in rows if r[0] == 400]
        ok = [r for r in rows if r[0] == 200]
        self.assertEqual(taken[0][1]["error"]["code"], "NAME_TAKEN")
        self.assertFalse(ok[0][1]["data"]["user"]["is_admin"])
        same = [u for u in self._users() if str(u["name"]).lower() == "raceruser"]
        self.assertEqual(len(same), 1)
        self.assertEqual(len({same[0]["id"]}), 1)

    def test_http_simultaneous_duplicate_emails_keep_one_row(self):
        seed_admin("bootstrapowner", "password1")
        os.environ["AUU_EMAIL_VERIFY"] = "on"
        import app.auth.accounts as accounts
        import app.auth.email_codes as email_codes

        barrier = threading.Barrier(2)
        original = accounts.hash_password

        def concurrent_hash(password):
            barrier.wait(timeout=10)
            return original(password)

        def signup(name: str):
            client = self.client.__class__(self.app)
            try:
                res = client.post(
                    "/api/v1/auth/register",
                    json={
                        "name": name,
                        "password": "password1",
                        "password_confirm": "password1",
                        "email": "same@example.com",
                        "email_code": "123456",
                    },
                )
                return res.status_code, res.json()
            finally:
                client.close()

        with mock.patch.object(accounts, "email_verify_enabled", return_value=True), mock.patch.object(
            email_codes, "check_code", return_value=None
        ), mock.patch.object(accounts, "check_code", return_value=None), mock.patch.object(
            accounts, "hash_password", concurrent_hash
        ):
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                rows = list(pool.map(signup, ["mailone", "mailtwo"]))
        statuses = sorted(r[0] for r in rows)
        self.assertEqual(statuses, [200, 400], rows)
        codes = {r[1]["error"]["code"] for r in rows if r[0] == 400}
        self.assertEqual(codes, {"EMAIL_TAKEN"})
        stored = [u for u in self._users() if u.get("email") == "same@example.com"]
        self.assertEqual(len(stored), 1)

    def test_cross_process_duplicate_names_keep_one_row(self):
        seed_admin("bootstrapowner", "password1")
        env = {key: os.environ.get(key, "") for key in _KEYS}
        env["AUU_USER_STORE"] = os.environ["AUU_USER_STORE"]
        env["AUU_USER_JOURNAL_DIR"] = os.environ["AUU_USER_JOURNAL_DIR"]
        out1 = Path(self.tmp.name) / "p1.json"
        out2 = Path(self.tmp.name) / "p2.json"
        ctx = get_context("spawn")
        procs = [
            ctx.Process(target=_child_register, args=(env, "procuser", str(out1))),
            ctx.Process(target=_child_register, args=(env, "PROCUSER", str(out2))),
        ]
        for proc in procs:
            proc.start()
        for proc in procs:
            proc.join(timeout=30)
            self.assertEqual(proc.exitcode, 0)
        results = [json.loads(path.read_text(encoding="utf-8")) for path in (out1, out2)]
        oks = [row for row in results if row.get("ok")]
        fails = [row for row in results if not row.get("ok")]
        self.assertEqual(len(oks), 1, results)
        self.assertEqual(len(fails), 1, results)
        self.assertEqual(fails[0]["code"], "NAME_TAKEN")
        self.assertFalse(oks[0]["is_admin"])
        same = [u for u in self._users() if str(u["name"]).lower() == "procuser"]
        self.assertEqual(len(same), 1)


    def test_issue_token_does_not_drop_a_concurrent_signup(self):
        """R1: an existing user's session write must reload so a just-registered user is kept."""
        owner = seed_admin("owner", "password1")
        ready = threading.Event()
        resume = threading.Event()
        import app.auth.accounts as accounts

        original = accounts._save_locked

        def paused(users):
            ready.set()
            self.assertTrue(resume.wait(10))
            return original(users)

        result = {}

        def write_session():
            with mock.patch.object(accounts, "_save_locked", paused):
                result["token"] = accounts.issue_token(owner["id"])

        def signup():
            self.assertTrue(ready.wait(10))
            # This should block until issue_token releases the store lock, then succeed.
            result["user"] = accounts.register("lateruser", "password1")

        t_token = threading.Thread(target=write_session)
        t_sign = threading.Thread(target=signup)
        t_token.start()
        self.assertTrue(ready.wait(10))
        t_sign.start()
        deadline = time.time() + 2
        while time.time() < deadline and "lateruser" not in {row["name"] for row in self._users()}:
            time.sleep(0.05)
        self.assertNotIn("lateruser", {row["name"] for row in self._users()})
        resume.set()
        t_token.join(10)
        t_sign.join(10)
        self.assertFalse(t_token.is_alive())
        self.assertFalse(t_sign.is_alive())
        persisted = {row["name"]: row for row in self._users()}
        self.assertIn("owner", persisted)
        self.assertIn("lateruser", persisted)
        self.assertFalse(persisted["lateruser"]["is_admin"])
        self.assertTrue(persisted["owner"]["sessions"])
        self.assertTrue(accounts.user_from_token(result["token"]))

    def test_locale_and_signup_lock_order_does_not_deadlock(self):
        """R2: every writer uses flock then RLock; locale + register must both finish."""
        owner = seed_admin("owner", "password1")
        import app.auth.accounts as accounts
        errors = []

        def set_loc():
            try:
                accounts.set_locale(owner["id"], "en")
            except Exception as exc:
                errors.append(exc)

        def signup():
            try:
                accounts.register("raceruser", "password1")
            except Exception as exc:
                errors.append(exc)

        ta = threading.Thread(target=set_loc, name="locale-A")
        tb = threading.Thread(target=signup, name="signup-B")
        ta.start()
        tb.start()
        ta.join(8)
        tb.join(8)
        self.assertFalse(ta.is_alive())
        self.assertFalse(tb.is_alive())
        self.assertEqual(errors, [])
        persisted = {row["name"]: row for row in self._users()}
        self.assertEqual(persisted["owner"].get("locale"), "en")
        self.assertIn("raceruser", persisted)

    def test_mixed_invalid_row_is_rejected_and_bytes_unchanged(self):
        """R3: a well-formed admin plus an invalid row must not be filtered or rewritten."""
        seed_admin("owner", "password1")
        payload = json.loads(self.store.read_text(encoding="utf-8"))
        payload["users"].append({"id": "legacy-or-broken-row", "is_admin": False})
        raw = json.dumps(payload)
        self.store.write_text(raw, encoding="utf-8")
        before = self.store.read_bytes()
        reset_accounts()
        me = self.client.get("/api/v1/auth/me")
        self.assertEqual(me.status_code, 200)
        self.assertFalse(me.json()["data"]["signup_allowed"])
        res = self.client.post(
            "/api/v1/auth/register",
            json={"name": "schemauser", "password": "password1", "password_confirm": "password1"},
        )
        self.assertEqual(res.status_code, 503, res.text)
        self.assertEqual(res.json()["error"]["code"], "STORE_UNAVAILABLE")
        self.assertEqual(self.store.read_bytes(), before)

    def test_wrong_field_type_and_duplicate_identity_fail_closed(self):
        seed_admin("owner", "password1")
        payload = json.loads(self.store.read_text(encoding="utf-8"))
        payload["users"][0]["is_admin"] = "yes"
        raw = json.dumps(payload)
        self.store.write_text(raw, encoding="utf-8")
        before = self.store.read_bytes()
        reset_accounts()
        res = self.client.post(
            "/api/v1/auth/register",
            json={"name": "typed", "password": "password1", "password_confirm": "password1"},
        )
        self.assertEqual(res.status_code, 503)
        self.assertEqual(res.json()["error"]["code"], "STORE_UNAVAILABLE")
        self.assertEqual(self.store.read_bytes(), before)
        # duplicates
        reset_accounts()
        seed = {"users": [
            {"id": "a1", "name": "alpha", "password_hash": "x", "is_admin": True},
            {"id": "a1", "name": "beta", "password_hash": "y", "is_admin": False},
        ]}
        raw = json.dumps(seed)
        self.store.write_text(raw, encoding="utf-8")
        before = self.store.read_bytes()
        reset_accounts()
        res = self.client.post(
            "/api/v1/auth/register",
            json={"name": "gamma", "password": "password1", "password_confirm": "password1"},
        )
        self.assertEqual(res.status_code, 503)
        self.assertEqual(self.store.read_bytes(), before)

    def test_historical_optional_fields_are_kept_on_later_writes(self):
        seed_admin("owner", "password1")
        payload = json.loads(self.store.read_text(encoding="utf-8"))
        payload["users"][0]["legacy_note"] = "keep-me"
        payload["users"][0]["display_name"] = "Owner"
        self.store.write_text(json.dumps(payload), encoding="utf-8")
        reset_accounts()
        import app.auth.accounts as accounts

        accounts.issue_token(payload["users"][0]["id"])
        accounts.set_locale(payload["users"][0]["id"], "ja")
        accounts.register("keptuser", "password1")
        rows = {row["name"]: row for row in self._users()}
        self.assertEqual(rows["owner"]["legacy_note"], "keep-me")
        self.assertEqual(rows["owner"]["display_name"], "Owner")
        self.assertEqual(rows["owner"]["locale"], "ja")
        self.assertIn("keptuser", rows)
