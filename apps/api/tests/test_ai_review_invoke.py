"""Click-to-call notes. The real relay transport is replaced and must not run."""
from __future__ import annotations

import json
import os
import socket
import tempfile
import threading
import unittest
import urllib.error
from pathlib import Path

from app.ai_review.invoke import clear_clicks
from app.ai_review.relay import RelayError, reset_budget
from app.auth.accounts import reset_accounts

PW = "reviewPass1"
LOOPS = {
    "AUU_SIGNAL_SHADOW": "off",
    "AUU_STRATEGY_RUNNER": "off",
    "AUU_SHADOW_S3": "off",
    "AUU_SHADOW_H2": "off",
    "AUU_ALERTS": "off",
    "AUU_RECON": "off",
    "AUU_UPTIME": "off",
    "AUU_PRICE_ALERTS": "off",
    "AUU_NEWS": "off",
    "AUU_AIRDROPS": "off",
    "AUU_AIRDROPS_INGEST": "off",
    "AUU_MAINSTREAM_REFRESH": "off",
    "AUU_RT_WS": "off",
    "PUMP_PAPER_LOOP": "0",
    "PUMPFUN_DISCOVERY": "off",
    "AUU_AI_REVIEW_FIXTURE": "on",
    "AUU_AI_REVIEW_MODEL": "off",
}
RELAY_KEYS = (
    "AUU_AI_RELAY",
    "AUU_AI_RELAY_API_KEY",
    "AUU_AI_RELAY_MODEL",
    "AUU_AI_RELAY_BUDGET",
    "AUU_AI_RELAY_CONCURRENCY",
    "AUU_AI_RELAY_DAY_FOR_TEST",
    "AUU_TEST",
)


def _refuse_network(*_args, **_kwargs):
    raise AssertionError("real relay transport is forbidden in tests")


class ClickTests(unittest.TestCase):
    def setUp(self):
        import app.ai_review.relay as relay

        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        keys = ("AUU_AUTH", "AUU_USER_STORE", "AUU_USER_JOURNAL_DIR", "AUU_ALLOW_SIGNUP", *LOOPS, *RELAY_KEYS)
        self._prev = {key: os.environ.get(key) for key in keys}
        os.environ["AUU_AUTH"] = "on"
        os.environ["AUU_ALLOW_SIGNUP"] = "on"
        os.environ["AUU_TEST"] = "1"
        os.environ["AUU_USER_STORE"] = str(root / "users.json")
        os.environ["AUU_USER_JOURNAL_DIR"] = str(root / "journals")
        for key, value in LOOPS.items():
            os.environ[key] = value
        for key in RELAY_KEYS:
            if key != "AUU_TEST":
                os.environ.pop(key, None)
        self._transport = relay.urllib_transport
        relay.urllib_transport = _refuse_network
        reset_accounts()
        reset_budget()
        clear_clicks()
        from fastapi.testclient import TestClient
        from app.main import app

        self.client_cls = TestClient
        self.app = app
        self.calls = {"n": 0, "body": b"", "url": "", "mode": "ok"}
        import app.ai_review.invoke as invoke

        self._invoke = invoke
        self._network_client = invoke.network_client
        import app.ai_review.relay as relay

        self._relay = relay
        self._dns = relay.socket.getaddrinfo
        relay.socket.getaddrinfo = lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("8.8.8.8", 443))]

    def tearDown(self):
        import app.ai_review.relay as relay

        relay.urllib_transport = self._transport
        self._relay.socket.getaddrinfo = self._dns
        self._invoke.network_client = self._network_client
        for key, value in self._prev.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        reset_budget()
        clear_clicks()
        reset_accounts()
        self.tmp.cleanup()

    def _user(self, name: str):
        client = self.client_cls(self.app)
        res = client.post("/api/v1/auth/register", json={"name": name, "password": PW, "password_confirm": PW})
        self.assertEqual(res.status_code, 200, res.text)
        return client

    def _ready(self, budget: str = "2") -> None:
        os.environ["AUU_AI_RELAY"] = "on"
        os.environ["AUU_AI_RELAY_API_KEY"] = "test-relay-key"
        os.environ["AUU_AI_RELAY_MODEL"] = "operator-selected"
        os.environ["AUU_AI_RELAY_BUDGET"] = budget
        reset_budget()
        clear_clicks()

    def _install_fake(self) -> None:
        from app.ai_review.relay import RelayClient

        def transport(url, payload, key, timeout):
            self.calls["n"] += 1
            self.calls["body"] = payload
            self.calls["url"] = url
            self.assertEqual(key, "test-relay-key")
            self.assertNotIn(b"test-relay-key", payload)
            if self.calls["mode"] == "down":
                raise urllib.error.URLError("down")
            if self.calls["mode"] == "timeout":
                raise TimeoutError("upstream accepted the request")
            if self.calls["mode"] == "badjson":
                return 200, {"choices": [{"message": {"content": "not-json"}}]}
            if self.calls["mode"] == "unverified":
                content = json.dumps({"summary": "Extra figure 99 is not stored", "citations": [{"key": "score", "value": 0.28}]})
                return 200, {"choices": [{"message": {"content": content}}]}
            if self.calls["mode"] == "long":
                return 200, {"choices": [{"message": {"content": "x" * 2001}}]}
            if self.calls["mode"] == "decision":
                content = json.dumps({"summary": "Stored row is not a live order.", "citations": [{"key": "live_order", "value": False}]})
                return 200, {"choices": [{"message": {"content": content}}]}
            content = json.dumps({"summary": "Stored score is 0.28.", "citations": [{"key": "score", "value": 0.28}]})
            return 200, {"choices": [{"message": {"content": content}}]}

        self._invoke.network_client = lambda: RelayClient(transport=transport)

    def _signal(self):
        return {"kind": "signal", "strategy": "cand_A", "symbol": "BTC", "asof": 1792022400000}

    def test_reads_do_not_call_and_a_closed_budget_does_not_call(self):
        admin = self._user("clickadmin")
        self._install_fake()
        self._ready(budget="0")
        self.assertEqual(admin.get("/api/v1/ai-review/plaza").status_code, 200)
        explained = admin.get("/api/v1/ai-review/signals/explain?strategy=cand_A&symbol=BTC&asof=1792022400000")
        self.assertEqual(explained.status_code, 200, explained.text)
        self.assertEqual(self.calls["n"], 0)
        blocked = admin.post("/api/v1/ai-review/interpret", json=self._signal(), headers={"Origin": "http://testserver"})
        self.assertEqual(blocked.status_code, 200, blocked.text)
        self.assertFalse(blocked.json()["data"]["sent"])
        self.assertFalse(blocked.json()["data"]["connected"])
        self.assertEqual(self.calls["n"], 0)
        self.assertNotIn("test-relay-key", blocked.text)

    def test_admin_click_sends_once_and_repeat_does_not_charge_again(self):
        from app.ai_review.relay import public_status

        admin = self._user("clickadmin")
        self._install_fake()
        self._ready()
        headers = {"Origin": "http://testserver"}
        first = admin.post("/api/v1/ai-review/interpret", json=self._signal(), headers=headers)
        self.assertEqual(first.status_code, 200, first.text)
        body = first.json()["data"]
        self.assertTrue(body["sent"])
        self.assertTrue(body["charged_now"])
        self.assertFalse(body["connected"])
        self.assertFalse(body["orders_enabled"])
        self.assertTrue(body["not_a_return"])
        self.assertTrue(body["not_an_order"])
        self.assertEqual(body["ai"]["text"], "Stored score is 0.28.")
        self.assertIsNone(body["budget"]["currency"])
        self.assertFalse(body["budget"]["guaranteed"])
        self.assertFalse(body["budget"]["price_known"])
        self.assertEqual(body["budget"]["unit"], "upstream_dispatch_attempt_per_utc_day")
        self.assertEqual(self.calls["n"], 1)
        self.assertEqual(self.calls["url"], "https://ai.orbitlink.me/v1/chat/completions")
        sent = json.loads(self.calls["body"])
        self.assertEqual(sent["model"], "operator-selected")
        self.assertNotEqual(sent["model"], "gpt-4o-mini")
        status = admin.get("/api/v1/ai-review/relay").json()["data"]
        self.assertEqual(status["verified_candidate"], "gpt-4o-mini")
        self.assertFalse(status["candidate_is_default"])
        self.assertEqual(status["model"], "operator-selected")
        second = admin.post("/api/v1/ai-review/interpret", json=self._signal(), headers=headers)
        self.assertEqual(second.status_code, 200, second.text)
        again = second.json()["data"]
        self.assertTrue(again["deduped"])
        self.assertFalse(again["charged_now"])
        self.assertFalse(again["sent"])
        self.assertEqual(self.calls["n"], 1)
        self.assertEqual(public_status()["budget_used"], 1)
        self.assertNotIn("total_return", first.text)

    def test_failure_is_not_counted_twice(self):
        from app.ai_review.relay import public_status

        admin = self._user("clickadmin")
        self._install_fake()
        self._ready()
        self.calls["mode"] = "down"
        headers = {"Origin": "http://testserver"}
        first = admin.post("/api/v1/ai-review/interpret", json=self._signal(), headers=headers)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertTrue(first.json()["data"]["charged_now"])
        self.assertEqual(first.json()["data"]["ai"]["reason"], "connection_error")
        self.assertEqual(public_status()["budget_used"], 1)
        second = admin.post("/api/v1/ai-review/interpret", json=self._signal(), headers=headers)
        self.assertTrue(second.json()["data"]["deduped"])
        self.assertFalse(second.json()["data"]["charged_now"])
        self.assertFalse(second.json()["data"]["sent"])
        self.assertEqual(self.calls["n"], 1)
        self.assertEqual(public_status()["budget_used"], 1)

    def test_overlong_output_is_not_counted(self):
        from app.ai_review.relay import public_status

        admin = self._user("clickadmin")
        self._install_fake()
        self._ready()
        self.calls["mode"] = "long"
        res = admin.post("/api/v1/ai-review/interpret", json=self._signal(), headers={"Origin": "http://testserver"})
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(res.json()["data"]["ai"]["reason"], "output_limit")
        self.assertTrue(res.json()["data"]["charged_now"])
        self.assertEqual(public_status()["budget_used"], 1)
        self.assertIsNone(res.json()["data"]["ai"]["text"])
        again = admin.post("/api/v1/ai-review/interpret", json=self._signal(), headers={"Origin": "http://testserver"})
        self.assertFalse(again.json()["data"]["sent"])
        self.assertEqual(self.calls["n"], 1)
        self.assertEqual(public_status()["budget_used"], 1)

    def test_cross_site_and_non_admin_cannot_send(self):
        admin = self._user("clickadmin")
        user = self._user("clickuser")
        anon = self.client_cls(self.app)
        self._install_fake()
        self._ready()
        evil = admin.post("/api/v1/ai-review/interpret", json=self._signal(), headers={"Origin": "https://evil.example"})
        self.assertEqual(evil.status_code, 403, evil.text)
        self.assertEqual(evil.json()["error"]["code"], "CSRF_ORIGIN")
        fetched = admin.post("/api/v1/ai-review/interpret", json=self._signal(), headers={"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(fetched.status_code, 403, fetched.text)
        self.assertEqual(fetched.json()["error"]["code"], "CSRF_ORIGIN")
        member = user.post("/api/v1/ai-review/interpret", json=self._signal(), headers={"Origin": "http://testserver"})
        self.assertEqual(member.status_code, 403, member.text)
        self.assertEqual(member.json()["error"]["code"], "ADMIN_REQUIRED")
        guest = anon.post("/api/v1/ai-review/interpret", json=self._signal(), headers={"Origin": "http://testserver"})
        self.assertEqual(guest.status_code, 401, guest.text)
        for res in (evil, fetched, member, guest):
            self.assertNotIn("score", res.text)
            self.assertNotIn("test-relay-key", res.text)
        self.assertEqual(self.calls["n"], 0)
        os.environ["AUU_AUTH"] = "off"
        closed = anon.post("/api/v1/ai-review/interpret", json=self._signal(), headers={"Origin": "http://testserver", "X-Admin": "true"})
        self.assertEqual(closed.status_code, 401, closed.text)
        self.assertEqual(closed.json()["error"]["code"], "AUTH_DISABLED")
        self.assertEqual(self.calls["n"], 0)

    def test_client_cannot_choose_the_model_and_decision_row_is_not_an_order(self):
        admin = self._user("clickadmin")
        self._install_fake()
        self._ready()
        headers = {"Origin": "http://testserver"}
        chosen = admin.post("/api/v1/ai-review/interpret", json={**self._signal(), "model": "gpt-4o-mini"}, headers=headers)
        self.assertEqual(chosen.status_code, 400, chosen.text)
        self.assertEqual(self.calls["n"], 0)
        listed = admin.get("/api/v1/ai-review/decisions")
        event_id = next(row["id"] for row in listed.json()["data"]["events"] if row.get("symbol") == "BTC")
        self.calls["mode"] = "decision"
        noted = admin.post("/api/v1/ai-review/interpret", json={"kind": "decision", "id": event_id}, headers=headers)
        self.assertEqual(noted.status_code, 200, noted.text)
        data = noted.json()["data"]
        self.assertTrue(data["sent"])
        self.assertTrue(data["not_an_order"])
        self.assertFalse(data["orders_enabled"])
        self.assertFalse(data["event"]["live_order"])
        self.assertEqual(data["ai"]["text"], "Stored row is not a live order.")
        self.assertEqual(self.calls["n"], 1)

    def test_dispatched_failures_stay_counted_across_a_new_reader(self):
        from app.ai_review.dispatch_quota import attempts_today, quota_path
        from app.ai_review.relay import _utc_day, public_status

        admin = self._user("clickadmin")
        self._install_fake()
        self._ready(budget="3")
        headers = {"Origin": "http://testserver"}
        self.calls["mode"] = "timeout"
        timed = admin.post("/api/v1/ai-review/interpret", json=self._signal(), headers=headers)
        self.assertEqual(timed.status_code, 200, timed.text)
        self.assertEqual(timed.json()["data"]["ai"]["reason"], "timeout")
        self.assertTrue(timed.json()["data"]["charged_now"])
        self.calls["mode"] = "badjson"
        # A second distinct dispatch is the decision row, after the signal attempt is kept.
        self.assertEqual(public_status()["budget_used"], 1)
        self.assertEqual(attempts_today(_utc_day()), 1)
        again = admin.post("/api/v1/ai-review/interpret", json=self._signal(), headers=headers)
        self.assertTrue(again.json()["data"]["deduped"])
        self.assertFalse(again.json()["data"]["sent"])
        self.assertEqual(self.calls["n"], 1)
        listed = admin.get("/api/v1/ai-review/decisions")
        event_id = next(row["id"] for row in listed.json()["data"]["events"] if row.get("symbol") == "BTC")
        self.calls["mode"] = "badjson"
        broken = admin.post("/api/v1/ai-review/interpret", json={"kind": "decision", "id": event_id}, headers=headers)
        self.assertEqual(broken.json()["data"]["ai"]["reason"], "bad_upstream")
        self.assertTrue(broken.json()["data"]["charged_now"])
        self.assertEqual(attempts_today(_utc_day()), 2)
        repeat = admin.post("/api/v1/ai-review/interpret", json={"kind": "decision", "id": event_id}, headers=headers)
        self.assertTrue(repeat.json()["data"]["deduped"])
        self.assertEqual(self.calls["n"], 2)
        self.assertEqual(attempts_today(_utc_day()), 2)
        blob = quota_path().read_bytes()
        self.assertNotIn(b"test-relay-key", blob)
        self.assertNotIn(b"You restate only", blob)
        self.assertNotIn(b"not-json", blob)

    def test_unchecked_model_text_counts_and_is_not_retried(self):
        from app.ai_review.relay import public_status

        admin = self._user("clickadmin")
        self._install_fake()
        self._ready()
        self.calls["mode"] = "unverified"
        headers = {"Origin": "http://testserver"}
        first = admin.post("/api/v1/ai-review/interpret", json=self._signal(), headers=headers)
        self.assertEqual(first.json()["data"]["ai"]["reason"], "unverified")
        self.assertIsNone(first.json()["data"]["ai"]["text"])
        self.assertTrue(first.json()["data"]["charged_now"])
        self.assertEqual(public_status()["budget_used"], 1)
        extra = admin.post(
            "/api/v1/ai-review/interpret",
            json={**self._signal(), "idempotency_key": "browser"},
            headers=headers,
        )
        self.assertEqual(extra.status_code, 400, extra.text)
        second = admin.post("/api/v1/ai-review/interpret", json=self._signal(), headers=headers)
        self.assertTrue(second.json()["data"]["deduped"])
        self.assertEqual(self.calls["n"], 1)
        self.assertEqual(public_status()["budget_used"], 1)

    def test_private_dns_and_busy_do_not_reserve(self):
        from app.ai_review.relay import end_begin, public_status, try_begin

        admin = self._user("clickadmin")
        self._install_fake()
        self._ready()
        self._relay.socket.getaddrinfo = lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("127.0.0.1", 443))]
        blocked = admin.post("/api/v1/ai-review/interpret", json=self._signal(), headers={"Origin": "http://testserver"})
        self.assertEqual(blocked.status_code, 200, blocked.text)
        self.assertFalse(blocked.json()["data"]["sent"])
        self.assertEqual(blocked.json()["data"]["ai"]["reason"], "host_not_allowed")
        self.assertEqual(self.calls["n"], 0)
        self.assertEqual(public_status()["budget_used"], 0)
        self._relay.socket.getaddrinfo = lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("8.8.8.8", 443))]
        self.assertTrue(try_begin())
        try:
            busy = admin.post("/api/v1/ai-review/interpret", json=self._signal(), headers={"Origin": "http://testserver"})
            self.assertEqual(busy.json()["data"]["ai"]["reason"], "busy")
            self.assertFalse(busy.json()["data"]["charged_now"])
            self.assertEqual(self.calls["n"], 0)
            self.assertEqual(public_status()["budget_used"], 0)
        finally:
            end_begin()

    def test_concurrency_and_utc_day_are_measured_without_a_dollar_cost(self):
        from app.ai_review.dispatch_quota import reserve
        from app.ai_review.relay import _utc_day, public_status

        self._ready(budget="1")
        day = _utc_day()
        barrier = threading.Barrier(2)
        found = []

        def go(key: str) -> None:
            barrier.wait(3)
            found.append((key, reserve(key, day, 1)))

        workers = [threading.Thread(target=go, args=(key,)) for key in ("aa" * 32, "bb" * 32)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(3)
        self.assertEqual(sum(1 for _key, row in found if row["ok"]), 1)
        self.assertEqual(public_status()["budget_used"], 1)
        self.assertIsNone(public_status()["budget_currency"])
        self.assertFalse(public_status()["budget_guaranteed"])
        self.assertFalse(public_status()["price_known"])
        winner = next(key for key, row in found if row["ok"])
        os.environ["AUU_AI_RELAY_DAY_FOR_TEST"] = "2099-01-02"
        self.assertEqual(public_status()["budget_used"], 0)
        repeated = reserve(winner, "2099-01-02", 1)
        self.assertFalse(repeated["ok"])
        self.assertEqual(repeated["reason"], "duplicate")
        self.assertEqual(public_status()["budget_used"], 0)
        fresh = reserve("cc" * 32, "2099-01-02", 1)
        self.assertTrue(fresh["ok"])
        self.assertEqual(public_status()["budget_used"], 1)


if __name__ == "__main__":
    unittest.main()
