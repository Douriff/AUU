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
# New relative to 1e9c986, and only when that path is absent from the baseline.
# 1e9c986 -> 2905d287 added these and changed no existing value. On current main
# they are already in the baseline, so a later edit is drift.
# nav.tabPnl is the exact mobile tab-bar short label added on 55fcd22; not nav.*.
_NEW_TREES = frozenset({"aiReview", "screener"})
_NEW_PATHS = frozenset({
    "apiErr.SCREENER_ADMIN",
    "nav.ai",
    "nav.tabPnl",
    "strat.k.awaitClose",
    "strat.k.gapHistory",
    "strat.k.gapReset",
    "strat.k.gapTitle",
    "strat.k.waitingClose",
    "strat.k.waitingData",
})


def _flat_locale(obj: dict) -> dict:
    out = {}

    def walk(node, prefix: str) -> None:
        for key, value in node.items():
            path = f"{prefix}.{key}" if prefix else key
            if isinstance(value, dict):
                walk(value, path)
            else:
                out[path] = value

    walk(obj, "")
    return out


def _addition_allowed(path: str, baseline: set[str]) -> bool:
    if path in _NEW_PATHS:
        return True
    # New strings inside the lab are allowed. A change to a lab string that
    # is already on main is still drift.
    if path.startswith("aiReview.") and path not in baseline:
        return True
    for tree in _NEW_TREES:
        if path == tree or path.startswith(tree + "."):
            return not any(old == tree or old.startswith(tree + ".") for old in baseline)
    return False


def locale_drift(old: dict, new: dict) -> list[str]:
    """Differences that are not an explicitly named addition absent from old."""
    old_paths, new_paths = _flat_locale(old), _flat_locale(new)
    problems = []
    removed = sorted(set(old_paths) - set(new_paths))
    if removed:
        problems.append("removed " + ", ".join(removed))
    added = sorted(set(new_paths) - set(old_paths))
    unknown = [path for path in added if not _addition_allowed(path, set(old_paths))]
    if unknown:
        problems.append("unexpected " + ", ".join(unknown))
    changed = sorted(path for path in set(old_paths) & set(new_paths) if old_paths[path] != new_paths[path])
    if changed:
        problems.append("changed " + ", ".join(changed))
    return problems


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
    def _baseline(self, lang: str) -> dict:
        raw = subprocess.check_output(["git", "show", f"main:apps/web/src/i18n/locales/{lang}.json"], cwd=ROOT)
        return json.loads(raw)

    def test_old_locale_keys_are_unchanged(self):
        for lang in LOCALES:
            old = self._baseline(lang)
            new = json.loads((ROOT / "apps/web/src/i18n/locales" / f"{lang}.json").read_text(encoding="utf-8"))
            self.assertEqual(locale_drift(old, new), [], lang)

    def test_changing_an_old_key_is_rejected(self):
        old = self._baseline("zh-CN")
        changed = json.loads(json.dumps(old))
        changed["strat"]["k"]["nav"] += "（已改）"
        drift = locale_drift(old, changed)
        self.assertTrue(any(item.startswith("changed strat.k.nav") for item in drift), drift)
        merged = json.loads(json.dumps(old))
        merged["apiErr"]["SCREENER_ADMIN"] += "（已改）"
        drift = locale_drift(old, merged)
        self.assertTrue(any("apiErr.SCREENER_ADMIN" in item for item in drift), drift)

    def test_only_named_additions_missing_from_the_baseline_are_allowed(self):
        old = {"keep": "原", "apiErr": {"BAD_LOGIN": "登录失败"}, "strat": {"k": {"nav": "净值"}}}
        added = json.loads(json.dumps(old))
        added["screener"] = {"title": "筛币"}
        added["apiErr"]["SCREENER_ADMIN"] = "预览"
        added["strat"]["k"]["waitingClose"] = "等待收盘"
        self.assertEqual(locale_drift(old, added), [])
        added["strat"]["k"]["nav"] = "改过"
        self.assertTrue(locale_drift(old, added))
        unknown = json.loads(json.dumps(old))
        unknown["brandNew"] = {"title": "不在名单"}
        self.assertTrue(locale_drift(old, unknown))
        tab = json.loads(json.dumps(old))
        tab["nav"] = {"tabPnl": "P&L"}
        self.assertEqual(locale_drift(old, tab), [])
        tab["nav"]["tabFoo"] = "nope"
        self.assertTrue(any("unexpected nav.tabFoo" in item for item in locale_drift(old, tab)), locale_drift(old, tab))

    def test_tab_pnl_short_labels_exist_and_desktop_pnl_is_unchanged(self):
        for lang in LOCALES:
            old = _flat_locale(self._baseline(lang))
            new = _flat_locale(json.loads((ROOT / "apps/web/src/i18n/locales" / f"{lang}.json").read_text(encoding="utf-8")))
            label = new.get("nav.tabPnl")
            self.assertIsInstance(label, str, lang)
            self.assertTrue(label.strip(), lang)
            self.assertEqual(new.get("nav.pnl"), old.get("nav.pnl"), lang)


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

    def test_public_status_never_contains_the_key_and_names_what_is_missing(self):
        from app.ai_review.relay import public_status

        os.environ["AUU_AI_RELAY"] = "on"
        self.assertEqual(public_status()["status"], "missing_key")
        os.environ["AUU_AI_RELAY_API_KEY"] = "test-relay-key"
        blob = json.dumps(public_status())
        self.assertNotIn("test-relay-key", blob)
        self.assertNotIn("api_key", blob)
        self.assertEqual(public_status()["status"], "missing_model")
        self.assertFalse(public_status()["invoked"])
        self.assertFalse(public_status()["live_site_verified"])
        self.assertEqual(public_status()["base_host"], "ai.orbitlink.me")
        os.environ["AUU_AI_RELAY_MODEL"] = "operator-selected"
        self.assertEqual(public_status()["status"], "budget_blocked")
        self.assertFalse(public_status()["invoked"])

    def test_unlimited_is_explicit_and_invalid_values_fail_closed(self):
        from app.ai_review.dispatch_quota import attempts_today, quota_path, reserve
        from app.ai_review.relay import _utc_day, parse_budget, public_status, reset_budget

        self.assertEqual(parse_budget(None)["limit"], 0)
        self.assertEqual(parse_budget("0")["limit"], 0)
        self.assertEqual(parse_budget("1000"), {"mode": "count", "limit": 1000, "unlimited": False})
        self.assertEqual(parse_budget("unlimited"), {"mode": "unlimited", "limit": "unlimited", "unlimited": True})
        for bad in ("1001", "none", "inf", "Unlimited", "-1", "1.5", "00", "+1000"):
            self.assertEqual(parse_budget(bad)["mode"], "invalid", bad)
            self.assertEqual(parse_budget(bad)["limit"], "invalid", bad)
        os.environ["AUU_AI_RELAY"] = "on"
        os.environ["AUU_AI_RELAY_API_KEY"] = "test-relay-key"
        os.environ["AUU_AI_RELAY_MODEL"] = "operator-selected"
        os.environ["AUU_AI_RELAY_BUDGET"] = "1001"
        invalid = public_status()
        self.assertEqual(invalid["status"], "budget_invalid")
        self.assertEqual(invalid["budget_limit"], "invalid")
        self.assertIsNone(invalid["budget_currency"])
        self.assertFalse(invalid["budget_guaranteed"])
        self.assertFalse(invalid["price_known"])
        os.environ["AUU_AI_RELAY_BUDGET"] = "1000"
        reset_budget()
        day = _utc_day()
        self.assertTrue(reserve("a" * 64, day, 1000)["ok"])
        import sqlite3
        con = sqlite3.connect(quota_path())
        con.execute("UPDATE day_attempts SET attempts=999 WHERE day=?", (day,))
        con.commit()
        con.close()
        self.assertTrue(reserve("b" * 64, day, 1000)["ok"])
        blocked = reserve("c" * 64, day, 1000)
        self.assertFalse(blocked["ok"])
        self.assertEqual(blocked["reason"], "budget")
        self.assertEqual(attempts_today(day), 1000)
        os.environ["AUU_AI_RELAY_BUDGET"] = "unlimited"
        open_day = public_status()
        self.assertEqual(open_day["budget_mode"], "unlimited")
        self.assertEqual(open_day["budget_limit"], "unlimited")
        self.assertTrue(open_day["budget_unlimited"])
        self.assertEqual(open_day["budget_used"], 1000)
        self.assertNotEqual(open_day["status"], "budget_blocked")
        self.assertTrue(reserve("d" * 64, day, None)["ok"])
        self.assertEqual(attempts_today(day), 1001)
        self.assertEqual(public_status()["budget_used"], 1001)
        import threading
        raced = []
        barrier = threading.Barrier(2)

        def go(key: str) -> None:
            barrier.wait(2)
            raced.append(reserve(key, day, None))

        workers = [threading.Thread(target=go, args=(key,)) for key in ("e" * 64, "f" * 64)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(3)
        self.assertEqual(sum(1 for row in raced if row["ok"]), 2)
        self.assertEqual(attempts_today(day), 1003)

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

    def test_transport_dials_the_checked_address_and_ignores_a_later_lookup(self):
        import app.ai_review.relay as relay

        lookups = {"n": 0}

        def lookup(host, port, type):
            lookups["n"] += 1
            if lookups["n"] == 1:
                return [(socket_family(), None, None, None, ("8.8.8.8", 443))]
            return [(socket_family(), None, None, None, ("127.0.0.1", 443))]

        seen = {}
        posts = {"n": 0}

        def post(address, host, payload, key, timeout):
            posts["n"] += 1
            seen["address"] = address
            seen["host"] = host
            return 200, {"ok": True}

        previous_lookup = relay.socket.getaddrinfo
        previous_post = relay._post_pinned
        relay.socket.getaddrinfo = lookup
        relay._post_pinned = post
        try:
            status, body = relay.urllib_transport(
                "https://ai.orbitlink.me/v1/chat/completions",
                b"{}",
                "test-relay-key",
                2,
            )
            self.assertEqual(status, 200)
            self.assertEqual(body, {"ok": True})
            self.assertEqual(seen["address"], "8.8.8.8")
            self.assertEqual(seen["host"], "ai.orbitlink.me")
            self.assertEqual(lookups["n"], 1)
            with self.assertRaises(relay.RelayError) as blocked:
                relay.urllib_transport("https://ai.orbitlink.me/v1/chat/completions", b"{}", "test-relay-key", 2)
            self.assertEqual(blocked.exception.reason, "host_not_allowed")
            self.assertEqual(posts["n"], 1)
            with self.assertRaises(relay.RelayError) as foreign:
                relay.urllib_transport("https://example.com/v1/chat/completions", b"{}", "test-relay-key", 2)
            self.assertEqual(foreign.exception.reason, "host_not_allowed")
            self.assertEqual(posts["n"], 1)
        finally:
            relay.socket.getaddrinfo = previous_lookup
            relay._post_pinned = previous_post

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

        from app.ai_review.dispatch_quota import reserve
        from app.ai_review.relay import RelayClient, RelayError, _utc_day, public_status

        fact = "ab" * 32
        self.assertTrue(reserve(fact, _utc_day(), 1)["ok"])
        client = RelayClient(transport=transport)
        parsed = client.complete({"score": 0.28}, timeout_s=2, reservation=fact)
        self.assertEqual(parsed["summary"], "Stored score is 0.28.")
        self.assertEqual(seen["url"], "https://ai.orbitlink.me/v1/chat/completions")
        self.assertNotIn(b"test-relay-key", seen["body"])
        self.assertEqual(public_status()["budget_used"], 1)
        self.assertFalse(public_status()["budget_guaranteed"])
        self.assertIsNone(public_status()["budget_currency"])
        self.assertFalse(public_status()["price_known"])
        again = RelayClient(transport=transport)
        with self.assertRaises(RelayError) as blocked:
            again.complete({"score": 0.28}, timeout_s=1, reservation=fact)
        self.assertEqual(blocked.exception.reason, "not_reserved")
        self.assertEqual(seen["url"], "https://ai.orbitlink.me/v1/chat/completions")
        self.assertEqual(public_status()["budget_used"], 1)

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
        auto = validate_draft({"intent": "live", "confirm": "auto_within_limits", "leverage": 3})
        self.assertTrue(auto["ok"])
        self.assertEqual(auto["draft"]["confirm"], "auto_within_limits")
        self.assertFalse(auto["orders_enabled"])
        self.assertFalse(auto["ai_may_send"])
        self.assertFalse(auto["armed"])
        self.assertEqual(auto["channel"], "not_connected")

    def test_prefix_write_stays_closed(self):
        previous = os.environ.get("AUU_AUTH")
        os.environ["AUU_AUTH"] = "off"
        try:
            from fastapi.testclient import TestClient
            from app.main import app

            res = TestClient(app).post("/api/v1/ai-review")
            self.assertEqual(res.status_code, 401)
            self.assertNotIn("total_return", res.text)
            self.assertNotIn("orders_enabled", res.text)
        finally:
            if previous is None:
                os.environ.pop("AUU_AUTH", None)
            else:
                os.environ["AUU_AUTH"] = previous


class NoteContractTests(unittest.TestCase):
    """The prompt must ask for the object the checker accepts.

    These fixtures are synthetic. They do not reserve or replay a stored fact.
    """

    FACTS = {
        "strategy_id": "auu_signal_v0",
        "symbol": "BTC",
        "ts": 1753920000000,
        "book": "shadow",
        "kind": "signal",
        "side": "long",
        "price": None,
        "cost": None,
        "net": None,
        "live_order": False,
        "attribution": "stored_row",
    }

    def test_prompt_states_the_contract_and_forbids_calendar_numbers(self):
        from app.ai_review.relay import chat_body

        body = chat_body(self.FACTS, "gpt-4o-mini", 400)
        text = body["messages"][0]["content"]
        self.assertIn("summary", text)
        self.assertIn("citations", text)
        self.assertIn("calendar date", text)
        self.assertNotIn("restate only the JSON facts", text)
        self.assertEqual(body["max_tokens"], 400)

    def test_fact_object_and_calendar_date_fail_closed(self):
        from app.ai_review.model_adapter import classify_model_output

        checked, reason = classify_model_output(self.FACTS, self.FACTS)
        self.assertIsNone(checked)
        self.assertEqual(reason, "wrong_keys")
        dated = {
            "summary": "The bar opened on 2025-07-31.",
            "citations": [{"key": "side", "value": "long"}],
        }
        checked, reason = classify_model_output(dated, self.FACTS)
        self.assertIsNone(checked)
        self.assertEqual(reason, "number_not_in_facts")
        typed = {
            "summary": "Stored side is long.",
            "citations": [{"key": "ts", "value": "1753920000000"}],
        }
        checked, reason = classify_model_output(typed, self.FACTS)
        self.assertIsNone(checked)
        self.assertEqual(reason, "citation_mismatch")

    def test_exact_citation_without_a_new_number_is_accepted(self):
        from app.ai_review.model_adapter import classify_model_output

        note = {
            "summary": "Stored side is long.",
            "citations": [{"key": "side", "value": "long"}, {"key": "live_order", "value": False}],
        }
        checked, reason = classify_model_output(note, self.FACTS)
        self.assertIsNone(reason)
        self.assertEqual(checked["summary"], "Stored side is long.")
        self.assertLess(len(json.dumps(note)), 400)


def socket_family():
    import socket

    return socket.AF_INET


if __name__ == "__main__":
    unittest.main()
