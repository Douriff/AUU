"""Integrity, relay, execution-draft, and locale-namespace checks for the review lab."""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import unittest
from pathlib import Path

from app.ai_review.explain import previous_bar
from app.ai_review.schema import BAR_READ_CAP
from app.data_paths import data_dir

ROOT = Path(__file__).resolve().parents[3]
LOCALES = ["zh-CN", "en", "de", "fr", "es", "pt", "tr", "ru", "ja", "ko", "ar"]


def _signal_db() -> sqlite3.Connection:
    path = data_dir() / "auu_signal_shadow.sqlite"
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE cand_nav (version TEXT, ts INTEGER, ret REAL, nav REAL, n_long INTEGER, n_short INTEGER, n_sym INTEGER, recorded_at INTEGER)"
    )
    conn.execute(
        "CREATE TABLE cand_bars (version TEXT, params_hash TEXT, symbol TEXT, tf TEXT, asof_ts INTEGER, side TEXT, score REAL, action TEXT, factors TEXT, recorded_at INTEGER)"
    )
    return conn


class TruncationTests(unittest.TestCase):
    def setUp(self):
        self._prev = os.environ.get("AUU_AI_REVIEW_FIXTURE")
        os.environ["AUU_AI_REVIEW_FIXTURE"] = "off"
        path = data_dir() / "auu_signal_shadow.sqlite"
        if path.exists():
            path.unlink()

    def tearDown(self):
        if self._prev is None:
            os.environ.pop("AUU_AI_REVIEW_FIXTURE", None)
        else:
            os.environ["AUU_AI_REVIEW_FIXTURE"] = self._prev

    def test_one_strategy_over_the_cap_does_not_invent_a_window(self):
        import app.ai_review.store_read as store

        previous = store.NAV_READ_CAP
        store.NAV_READ_CAP = 2
        try:
            conn = _signal_db()
            for index, ret in enumerate((0.01, -0.02, 0.03)):
                nav = 1.0
                # not a real chain; the point is that a partial read must not be published
                conn.execute(
                    "INSERT INTO cand_nav VALUES ('cand_A',?,?,?,1,0,1,1)",
                    (1_700_000_000_000 + index * 86_400_000, ret, 1 + ret),
                )
            conn.execute(
                "INSERT INTO cand_nav VALUES ('cand_B',?,?,?,1,0,1,1)",
                (1_700_000_000_000, 0.02, 1.02),
            )
            conn.commit()
            conn.close()
            from app.ai_review.service import plaza_payload

            payload = plaza_payload(now_ms=1_700_000_000_000)
            by_id = {row["id"]: row for row in payload["strategies"]}
            self.assertEqual(by_id["cand_A"]["performance"]["status"], "incomplete")
            self.assertEqual(by_id["cand_A"]["performance"]["reason"], "truncated")
            self.assertIsNone(by_id["cand_A"]["performance"]["total_return"])
            self.assertIsNone(by_id["cand_A"]["performance"]["max_drawdown"])
            self.assertEqual(by_id["cand_B"]["performance"]["status"], "ok")
            self.assertAlmostEqual(by_id["cand_B"]["performance"]["total_return"], 0.02, places=6)
        finally:
            store.NAV_READ_CAP = previous


class FillAttributionTests(unittest.TestCase):
    def setUp(self):
        os.environ["AUU_AI_REVIEW_FIXTURE"] = "off"
        path = data_dir() / "mainstream_strategy.sqlite"
        if path.exists():
            path.unlink()

    def test_fills_follow_the_run_on_that_day_and_orphans_stay_unassigned(self):
        path = data_dir() / "mainstream_strategy.sqlite"
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE runs (day INTEGER PRIMARY KEY, strategy TEXT, ran_at INTEGER, nav_close REAL, ret REAL, cost REAL, funding REAL)")
        conn.execute("CREATE TABLE fills (day INTEGER, coin TEXT, side TEXT, price REAL, fill_price REAL, fee REAL, slippage REAL)")
        conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO meta VALUES ('start_nav', '10000')")
        conn.execute("INSERT INTO runs VALUES (1000, 'trend_tsmom_v1', 1000, 10100, 0.01, 1, 0)")
        conn.execute("INSERT INTO runs VALUES (2000, 'other_book', 2000, 9900, -0.01, 1, 0)")
        conn.execute("INSERT INTO fills VALUES (1000, 'BTC', 'buy', 10, 10, 0.1, 0.1)")
        conn.execute("INSERT INTO fills VALUES (2000, 'ETH', 'sell', 20, 20, 0.2, 0.2)")
        conn.execute("INSERT INTO fills VALUES (3000, 'SOL', 'buy', 5, 5, 0.1, 0.1)")
        conn.commit()
        conn.close()
        from app.ai_review.service import plaza_payload

        events = plaza_payload(now_ms=4000)["decisions"]
        by_symbol = {row["symbol"]: row for row in events if row["kind"] == "fill"}
        self.assertEqual(by_symbol["BTC"]["strategy_id"], "trend_tsmom_v1")
        self.assertEqual(by_symbol["BTC"]["attribution"], "run_day")
        self.assertEqual(by_symbol["ETH"]["strategy_id"], "other_book")
        self.assertIsNone(by_symbol["SOL"]["strategy_id"])
        self.assertEqual(by_symbol["SOL"]["attribution"], "no_matching_run")


class SignalPageTests(unittest.TestCase):
    def setUp(self):
        os.environ["AUU_AI_REVIEW_FIXTURE"] = "off"
        path = data_dir() / "auu_signal_shadow.sqlite"
        if path.exists():
            path.unlink()

    def test_limit_is_applied_to_the_stored_rows(self):
        conn = _signal_db()
        for index in range(20):
            conn.execute(
                "INSERT INTO cand_bars VALUES ('cand_A','25fad2f2e60c7325','BTC','1d',?,?,?,?,?,?)",
                (1_700_000_000_000 + index * 86_400_000, "flat", 0.1, "none", "{}", 1),
            )
        conn.commit()
        conn.close()
        from app.ai_review.service import signals_payload

        short = signals_payload("cand_A", "BTC", limit=5)
        self.assertEqual(len(short["signals"]), 5)
        self.assertEqual(short["stored"], 20)
        self.assertTrue(short["truncated"])
        full = signals_payload("cand_A", "BTC", limit=50)
        self.assertEqual(len(full["signals"]), 20)
        self.assertFalse(full["truncated"])
        self.assertLessEqual(BAR_READ_CAP, 2000)


class GapTests(unittest.TestCase):
    def test_hourly_gap_is_one_bar_not_a_day_floor(self):
        hour = 3_600_000
        rows = [{"asof_ts": 0, "side": "flat"}, {"asof_ts": 3 * hour, "side": "long"}]
        _prev, gap = previous_bar(rows, 3 * hour, hour)
        self.assertTrue(gap)
        _prev, ok = previous_bar(rows, hour, hour)
        self.assertFalse(ok)

    def test_v0_replay_is_not_a_verified_parameter_binding(self):
        from app.ai_review.catalog import identity
        from app.ai_review.explain import explain_bar

        ident = identity("auu_signal_v0")
        prev = {"asof_ts": 0, "side": "flat", "score": 0.0, "action": "none", "factors": {"momentum": 0.0, "volume_imbalance": 0.0, "funding": 0.0}, "reason_tags": []}
        bar = {"symbol": "BTC", "asof_ts": 86_400_000, "side": "long", "score": 0.4125, "action": "enter",
               "factors": {"momentum": 0.6, "volume_imbalance": 0.1, "funding": None}, "reason_tags": ["momentum"], "params_hash": None}
        explained = explain_bar(ident, bar, prev, gap=False)
        self.assertEqual(explained["reason"], "params_hash_not_stored")
        self.assertFalse(explained["params_hash_verified"])
        self.assertEqual(explained["params_binding"], "version_string_only")
        self.assertNotEqual(explained["reconstructed"], "yes")


class LocaleNamespaceTests(unittest.TestCase):
    def test_old_locale_keys_are_unchanged(self):
        for lang in LOCALES:
            old = json.loads(subprocess.check_output(["git", "show", f"main:apps/web/src/i18n/locales/{lang}.json"], cwd=ROOT))
            new = json.loads((ROOT / "apps/web/src/i18n/locales" / f"{lang}.json").read_text(encoding="utf-8"))
            self.assertEqual(set(new) - set(old), {"aiReview"}, lang)
            old.pop("aiReview", None)
            new.pop("aiReview", None)
            self.assertEqual(old, new, lang)


class RelayAndDraftTests(unittest.TestCase):
    def setUp(self):
        from app.ai_review.relay import reset_budget

        self._env = {key: os.environ.get(key) for key in ("AUU_AI_RELAY", "AUU_AI_RELAY_API_KEY", "AUU_AI_RELAY_MODEL", "AUU_AI_RELAY_BUDGET", "AUU_AI_RELAY_BASE")}
        for key in self._env:
            os.environ.pop(key, None)
        reset_budget()

    def tearDown(self):
        from app.ai_review.relay import reset_budget

        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        reset_budget()

    def test_public_status_never_contains_the_key_and_does_not_claim_a_call(self):
        os.environ["AUU_AI_RELAY_API_KEY"] = "test-relay-key"
        from app.ai_review.relay import public_status

        blob = json.dumps(public_status())
        self.assertNotIn("test-relay-key", blob)
        self.assertNotIn("api_key", blob)
        self.assertFalse(public_status()["invoked"])
        self.assertFalse(public_status()["live_site_verified"])
        self.assertEqual(public_status()["base_host"], "ai.orbitlink.me")

    def test_private_address_and_redirect_are_refused_before_a_second_request(self):
        from app.ai_review.relay import RelayError, assert_public_host, interpret_status, normalize_base

        with self.assertRaises(RelayError):
            normalize_base("http://127.0.0.1/v1")
        with self.assertRaises(RelayError):
            normalize_base("https://example.com/v1")
        self.assertEqual(normalize_base("https://ai.orbitlink.me/v1"), "https://ai.orbitlink.me/v1")
        with self.assertRaises(RelayError) as redirected:
            interpret_status(302)
        self.assertEqual(redirected.exception.reason, "redirect_refused")

        def fake_lookup(host, port, type):
            return [(socket_family(), None, None, None, ("127.0.0.1", 443))]

        import app.ai_review.relay as relay

        previous = relay.socket.getaddrinfo
        relay.socket.getaddrinfo = fake_lookup
        try:
            with self.assertRaises(RelayError) as blocked:
                assert_public_host("ai.orbitlink.me")
        finally:
            relay.socket.getaddrinfo = previous
        self.assertEqual(blocked.exception.reason, "host_not_allowed")

    def test_enabled_client_uses_the_transport_once_and_rejects_a_rewritten_number(self):
        os.environ["AUU_AI_RELAY"] = "on"
        os.environ["AUU_AI_RELAY_API_KEY"] = "test-relay-key"
        os.environ["AUU_AI_RELAY_MODEL"] = "operator-selected"
        os.environ["AUU_AI_RELAY_BUDGET"] = "1"
        seen = {}

        def transport(url, payload, key, timeout):
            seen["url"] = url
            seen["key"] = key
            seen["body"] = payload
            content = json.dumps({"summary": "Stored score is 0.28.", "citations": [{"key": "score", "value": 0.28}]})
            return 200, {"choices": [{"message": {"content": content}}]}

        from app.ai_review.relay import RelayClient, public_status

        client = RelayClient(transport=transport)
        parsed = client.complete({"score": 0.28}, timeout_s=2)
        self.assertEqual(parsed["summary"], "Stored score is 0.28.")
        self.assertEqual(seen["url"], "https://ai.orbitlink.me/v1/chat/completions")
        self.assertNotIn(b"test-relay-key", seen["body"])
        self.assertTrue(public_status()["invoked"])
        self.assertFalse(public_status()["live_site_verified"])
        again = RelayClient(transport=transport)
        from app.ai_review.relay import RelayDisabled

        with self.assertRaises(RelayDisabled):
            again.complete({"score": 0.28}, timeout_s=1)

    def test_execution_draft_rejects_secrets_and_stays_disarmed(self):
        from app.ai_review.execution import validate_draft

        blank = validate_draft({})
        self.assertTrue(blank["ok"])
        self.assertFalse(blank["armed"])
        self.assertEqual(blank["channel"], "not_connected")
        self.assertIsNone(blank["draft"]["capital_limit"])
        self.assertIsNone(blank["draft"]["leverage"])
        secret = validate_draft({"exchange_key": "abc", "intent": "live"})
        self.assertFalse(secret["ok"])
        self.assertEqual(secret["reason"], "secret_rejected")
        self.assertFalse(secret["armed"])


def socket_family():
    import socket

    return socket.AF_INET


if __name__ == "__main__":
    unittest.main()
