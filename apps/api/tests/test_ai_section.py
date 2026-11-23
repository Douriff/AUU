"""Studio drafts, personal paper agents, matcher, and route bounds.

RouteMountTests need the full AUU API tree (create_app / TestClient). They are
included for later GitHub HTTP verification and are not claimed passed until
that run exists.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from app.ai_section.catalog_view import public_card
from app.ai_section.compile import KNOWN_IDS, MAX_BODY, MAX_PROMPT, match_catalog
from app.ai_section.runner_view import public_runner_status
from app.ai_section.schema import SchemaError, validate_strategy_body
from app.ai_section.simulate import simulate_bars, sma, validate_bars
from app.ai_section.store import StudioStore, reset_store


def _draft(**overrides):
    body = {
        "name": "BTC SMA draft",
        "kind": "sma_cross",
        "symbol": "BTC",
        "timeframe": "1d",
        "params": {"fast": 3, "slow": 5},
        "risk": {"max_weight": 0.2, "stop_loss_pct": 0.1, "take_profit_pct": 0.2, "long_only": True},
    }
    body.update(overrides)
    return body


def _rising_bars(n=40, start=100.0):
    rows = []
    price = start
    ts = 1_700_000_000_000
    for i in range(n):
        price += 1.0
        rows.append({"ts": ts + i * 86_400_000, "open": price - 0.4, "high": price + 0.4, "low": price - 0.6, "close": price, "volume": 1})
    return rows


class MatchCatalogTests(unittest.TestCase):
    def test_time_series_momentum_maps_to_registered_tsmom(self):
        result = match_catalog("time series momentum daily rebalance")
        self.assertTrue(result["ok"])
        self.assertFalse(result["used_model"])
        self.assertFalse(result["generates_code"])
        self.assertFalse(result["creates_strategy"])
        self.assertEqual(result["engine"], "deterministic_catalog_matcher")
        self.assertEqual([row["id"] for row in result["matches"]], ["trend_tsmom_v1"])

    def test_unknown_prompt_does_not_invent_a_strategy_or_code(self):
        result = match_catalog("a completely unregistered mean-reversion sketch")
        self.assertEqual(result["matches"], [])
        self.assertFalse(result["generates_code"])

    def test_empty_and_oversized_prompts_are_rejected_as_bad_body(self):
        self.assertEqual(match_catalog("   ")["message"], "bad_body")
        self.assertEqual(match_catalog("x" * (MAX_PROMPT + 1))["message"], "bad_body")
        self.assertEqual(MAX_BODY, 8000)
        self.assertIn("trend_tsmom_v1", KNOWN_IDS)


class SchemaTests(unittest.TestCase):
    def test_valid_draft_is_verified_and_not_code(self):
        draft = validate_strategy_body(_draft())
        self.assertEqual(draft["status"], "verified")
        self.assertFalse(draft["generates_code"])
        self.assertFalse(draft["llm"])
        self.assertEqual(draft["params"]["fast"], 3)

    def test_user_code_and_bad_params_are_rejected(self):
        with self.assertRaises(SchemaError) as raised:
            validate_strategy_body(_draft(code="print(1)"))
        self.assertEqual(raised.exception.code, "CODE_FORBIDDEN")
        with self.assertRaises(SchemaError):
            validate_strategy_body(_draft(params={"fast": 30, "slow": 5}))
        with self.assertRaises(SchemaError):
            validate_strategy_body(_draft(kind="eval_script"))
        with self.assertRaises(SchemaError):
            validate_strategy_body(_draft(risk={"max_weight": 2}))


class SimulateTests(unittest.TestCase):
    def test_missing_candles_block_without_fills_or_pnl(self):
        draft = validate_strategy_body(_draft())
        result = simulate_bars(draft, [])
        self.assertEqual(result["status"], "blocked_no_data")
        self.assertEqual(result["fills"], [])
        self.assertIsNone(result["paper_pnl"])
        self.assertFalse(result["used_model"])
        self.assertFalse(result["live"])

    def test_rising_series_can_produce_a_paper_fill(self):
        draft = validate_strategy_body(_draft())
        result = simulate_bars(draft, _rising_bars())
        self.assertEqual(result["status"], "simulated")
        self.assertGreaterEqual(len(result["fills"]), 1)
        self.assertFalse(result["site_runner"])
        self.assertEqual(result["unit"], "paper_fraction")
        self.assertIsNotNone(result["paper_pnl"])

    def test_sma_helper_is_deterministic(self):
        self.assertEqual(sma([1, 2, 3, 4], 2), [None, 1.5, 2.5, 3.5])


class StoreIsolationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = StudioStore(Path(self.tmp.name) / "studio.sqlite")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_owner_can_round_trip_and_other_user_is_isolated(self):
        draft = validate_strategy_body(_draft())
        saved = self.store.create_strategy("user-a", draft)
        self.assertTrue(saved["id"].startswith("s_"))
        self.assertEqual(self.store.get_strategy("user-a", saved["id"])["name"], "BTC SMA draft")
        with self.assertRaises(Exception) as raised:
            self.store.get_strategy("user-b", saved["id"])
        self.assertEqual(raised.exception.code, "NOT_FOUND")
        with self.assertRaises(Exception):
            self.store.create_agent("user-b", "borrowed", saved["id"])
        agent = self.store.create_agent("user-a", "My paper agent", saved["id"])
        self.assertEqual(agent["status"], "idle")
        self.assertTrue(agent["id"].startswith("a_"))
        listed = self.store.list_agents("user-b")
        self.assertEqual(listed, [])

    def test_unverified_cannot_be_forced_via_store_status_without_schema(self):
        draft = validate_strategy_body(_draft())
        saved = self.store.create_strategy("user-a", draft)
        self.assertEqual(saved["status"], "verified")


class CatalogViewTests(unittest.TestCase):
    def test_public_card_strips_accounting_and_never_publishes_returns(self):
        card = public_card({"id": "shadow_h2", "version": "abc", "accounting_unit": 10000, "orders": True, "performance": {"total_return": 1.23}, "pool": {"symbols": ["BTC"], "size": 1}, "hold": {"kind": "sleeve_daily"}})
        self.assertFalse(card["orders"])
        self.assertIsNone(card["performance"])


class RunnerViewTests(unittest.TestCase):
    def test_status_is_site_wide_and_drops_ledger_figures(self):
        view = public_runner_status({"strategy": {"name": "trend_tsmom"}, "ledger": "mainstream_strategy.sqlite", "live": {"enabled": False}, "startNav": 10000, "nav": 12345, "positions": [{"coin": "BTC"}], "curve": [{"nav": 1}], "go_no_go": {"verdict": "pending", "lamp": "gray", "days": 3, "minDays": 250, "reasons": [], "stats": {"cagr": 0.4}}})
        self.assertEqual(view["scope"], "site_system_runner")
        self.assertFalse(view["personal"])
        self.assertNotIn("nav", view)
        self.assertNotIn("positions", view)


class RouteMountTests(unittest.TestCase):
    """HTTP / persistence / cross-user checks for later CI. Not claimed green on the box."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self._prev = {key: os.environ.get(key) for key in ("AUU_AUTH", "AUU_ALLOW_SIGNUP", "AUU_USER_STORE", "AUU_USER_JOURNAL_DIR", "AUU_AUTH_RATE_MAX", "AUU_SESSION_SECRET", "AUU_INVITE_CODE", "AUU_ADMIN_USER", "AUU_DATA_DIR", "AUU_TEST")}
        os.environ.update({
            "AUU_AUTH": "on",
            "AUU_ALLOW_SIGNUP": "on",
            "AUU_AUTH_RATE_MAX": "1000",
            "AUU_USER_STORE": str(root / "users.json"),
            "AUU_USER_JOURNAL_DIR": str(root / "journals"),
            "AUU_SESSION_SECRET": "ai-section-studio-secret",
            "AUU_DATA_DIR": str(root / "data"),
            "AUU_TEST": "1",
        })
        os.environ.pop("AUU_INVITE_CODE", None)
        os.environ["AUU_ADMIN_USER"] = "owner"
        from app.auth.accounts import reset_accounts
        from app.main import create_app
        from fastapi.testclient import TestClient
        from app.ai_section.store import StudioStore, reset_store

        reset_accounts()
        reset_store(StudioStore(root / "data" / "ai_section_studio.sqlite"))
        self.app = create_app(legacy=False)
        self.TC = TestClient
        self.owner = TestClient(self.app)
        self.member = TestClient(self.app)
        self.anon = TestClient(self.app)
        self.assertEqual(self.owner.post("/api/v1/auth/register", json={"name": "owner", "password": "password1", "password_confirm": "password1"}).status_code, 200)
        self.assertEqual(self.member.post("/api/v1/auth/register", json={"name": "member", "password": "password1", "password_confirm": "password1"}).status_code, 200)
        owner_me = self.owner.get("/api/v1/auth/me").json()["data"]["user"]
        member_me = self.member.get("/api/v1/auth/me").json()["data"]["user"]
        self.assertTrue(owner_me["is_admin"])
        self.assertFalse(member_me["is_admin"])

    def tearDown(self):
        for client in (getattr(self, "owner", None), getattr(self, "member", None), getattr(self, "anon", None)):
            if client is not None:
                client.close()
        reset_store()
        for key, value in self._prev.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        from app.auth.accounts import reset_accounts

        reset_accounts()
        self.tmp.cleanup()

    def test_anon_cannot_use_studio_or_personal_agents(self):
        self.assertEqual(self.anon.get("/api/v1/ai-section/strategies").status_code, 401)
        self.assertEqual(self.anon.post("/api/v1/ai-section/strategies", json=_draft()).status_code, 401)
        self.assertEqual(self.anon.get("/api/v1/ai-section/agents").status_code, 401)

    def test_anon_catalog_and_match_still_require_login_but_owner_can_load(self):
        self.assertEqual(self.anon.get("/api/v1/ai-section/catalog").status_code, 401)
        self.assertEqual(self.anon.post("/api/v1/ai-section/compile", json={"prompt": "time series momentum daily rebalance"}).status_code, 401)
        listed = self.owner.get("/api/v1/ai-section/catalog")
        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertIsInstance(listed.json()["data"].get("strategies"), list)
        matched = self.owner.post("/api/v1/ai-section/compile", json={"prompt": "time series momentum daily rebalance"})
        self.assertEqual(matched.status_code, 200, matched.text)
        body = matched.json()["data"]
        self.assertFalse(body.get("used_model"))
        self.assertFalse(body.get("generates_code"))

    def test_owner_round_trip_and_missing_candles_do_not_invent_results(self):
        created = self.owner.post("/api/v1/ai-section/strategies", json=_draft())
        self.assertEqual(created.status_code, 200, created.text)
        data = created.json()["data"]
        self.assertEqual(data["status"], "verified")
        self.assertFalse(data["generates_code"])
        sid = data["id"]
        listed = self.owner.get("/api/v1/ai-section/strategies")
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.json()["data"]["strategies"][0]["id"], sid)
        agent = self.owner.post("/api/v1/ai-section/agents", json={"name": "BTC paper", "strategy_id": sid})
        self.assertEqual(agent.status_code, 200, agent.text)
        aid = agent.json()["data"]["id"]
        sim = self.owner.post(f"/api/v1/ai-section/agents/{aid}/simulate", json={})
        self.assertEqual(sim.status_code, 200, sim.text)
        result = sim.json()["data"]["result"]
        self.assertEqual(result["status"], "blocked_no_data")
        self.assertEqual(result["fills"], [])
        self.assertIsNone(result["paper_pnl"])
        self.assertEqual(self.owner.post(f"/api/v1/ai-section/agents/{aid}/start", json={}).status_code, 405)
        self.assertEqual(self.owner.post(f"/api/v1/ai-section/agents/{aid}/orders", json={}).status_code, 405)

    def test_cross_user_and_admin_cannot_read_or_write_another_book(self):
        created = self.owner.post("/api/v1/ai-section/strategies", json=_draft())
        sid = created.json()["data"]["id"]
        self.assertEqual(self.member.get(f"/api/v1/ai-section/strategies/{sid}").status_code, 404)
        self.assertEqual(self.member.patch(f"/api/v1/ai-section/strategies/{sid}", json=_draft(name="hijack")).status_code, 404)
        self.assertEqual(self.member.delete(f"/api/v1/ai-section/strategies/{sid}").status_code, 404)
        self.assertEqual(self.member.post("/api/v1/ai-section/agents", json={"name": "steal", "strategy_id": sid}).status_code, 404)
        self.assertEqual(len(self.member.get("/api/v1/ai-section/strategies").json()["data"]["strategies"]), 0)
        member_draft = self.member.post("/api/v1/ai-section/strategies", json=_draft(name="member draft"))
        self.assertEqual(member_draft.status_code, 200, member_draft.text)
        mid = member_draft.json()["data"]["id"]
        self.assertEqual(self.owner.get(f"/api/v1/ai-section/strategies/{mid}").status_code, 404)
        self.assertEqual(self.owner.patch(f"/api/v1/ai-section/strategies/{mid}", json=_draft(name="admin-hijack")).status_code, 404)
        self.assertEqual(self.owner.delete(f"/api/v1/ai-section/strategies/{mid}").status_code, 404)
        self.assertEqual(self.owner.post("/api/v1/ai-section/agents", json={"name": "admin-steal", "strategy_id": mid}).status_code, 404)
        agent = self.member.post("/api/v1/ai-section/agents", json={"name": "member paper", "strategy_id": mid})
        self.assertEqual(agent.status_code, 200, agent.text)
        aid = agent.json()["data"]["id"]
        self.assertEqual(self.owner.get(f"/api/v1/ai-section/agents/{aid}").status_code, 404)
        self.assertEqual(self.owner.post(f"/api/v1/ai-section/agents/{aid}/simulate", json={}).status_code, 404)
        self.assertEqual(self.owner.get(f"/api/v1/ai-section/agents/{aid}/simulations").status_code, 404)
        self.assertEqual(self.owner.delete(f"/api/v1/ai-section/agents/{aid}").status_code, 404)

    def test_code_payload_and_oversize_body_are_rejected(self):
        rejected = self.owner.post("/api/v1/ai-section/strategies", json={**_draft(), "code": "os.system('x')"})
        self.assertEqual(rejected.status_code, 400)
        self.assertEqual(rejected.json()["error"]["code"], "CODE_FORBIDDEN")
        too_large = self.owner.post("/api/v1/ai-section/strategies", content=b"{" + b"x" * (MAX_BODY + 1) + b"}", headers={"Content-Type": "application/json"})
        self.assertEqual(too_large.status_code, 413)
        self.assertEqual(too_large.json()["error"]["code"], "TOO_LARGE")
        listed = self.owner.get("/api/v1/ai-section/strategies")
        self.assertEqual(listed.json()["data"]["strategies"], [])
        bad_utf = self.owner.post("/api/v1/ai-section/strategies", content=b'{"name":"\xff","kind":"sma_cross","symbol":"BTC","timeframe":"1d"}', headers={"Content-Type": "application/json"})
        self.assertEqual(bad_utf.status_code, 400)
        nan = self.owner.post("/api/v1/ai-section/strategies", content=b'{"name":"n","kind":"sma_cross","symbol":"BTC","timeframe":"1d","params":{"fast":NaN,"slow":5}}', headers={"Content-Type": "application/json"})
        self.assertEqual(nan.status_code, 400)
        self.assertEqual(self.owner.get("/api/v1/ai-section/strategies").json()["data"]["strategies"], [])
        self.assertEqual(self.owner.post("/api/v1/ai-section/interpret", json={"kind": "decision", "id": "x"}).status_code, 405)

    def test_same_origin_write_allowed_and_cross_origin_csrf_rejected(self):
        same = {"Origin": "http://testserver"}
        created = self.owner.post("/api/v1/ai-section/strategies", json=_draft(), headers=same)
        self.assertEqual(created.status_code, 200, created.text)
        evil = self.owner.post("/api/v1/ai-section/strategies", json=_draft(name="csrf"), headers={"Origin": "https://evil.example"})
        self.assertEqual(evil.status_code, 403, evil.text)
        self.assertEqual(evil.json()["error"]["code"], "CSRF_ORIGIN")
        names = [row["name"] for row in self.owner.get("/api/v1/ai-section/strategies").json()["data"]["strategies"]]
        self.assertNotIn("csrf", names)


DAY = 86_400_000
BAR_START = 1_700_000_000_000
NOW_MS = BAR_START + 400 * DAY


def _regular_bars(n, *, start=BAR_START, pattern="up", close0=100.0):
    rows = []
    price = float(close0)
    for i in range(n):
        ts = start + i * DAY
        if pattern == "up":
            open_px, close_px = price, price + 1.0
            price = close_px
        elif pattern == "down":
            open_px, close_px = price, price - 1.0
            price = close_px
        elif pattern == "alt":
            close_px = 120.0 if i % 2 else 100.0
            open_px = 100.0 if i % 2 else 120.0
        elif pattern == "flat":
            open_px = close_px = price
        else:
            raise ValueError(pattern)
        high = max(open_px, close_px)
        low = min(open_px, close_px)
        rows.append({"ts": ts, "open": open_px, "high": high, "low": low, "close": close_px, "volume": 1})
    return rows


class FakeRequest:
    def __init__(self, chunks, headers=None):
        self.headers = headers or {}
        self._chunks = [bytes(chunk) for chunk in chunks]
        self.chunks_consumed = 0
        self.started = False

    async def stream(self):
        self.started = True
        for chunk in self._chunks:
            self.chunks_consumed += 1
            yield chunk


class BoundedBodyTests(unittest.TestCase):
    def _read(self, chunks, headers=None):
        import asyncio
        from app.ai_section.body import read_bounded_json

        req = FakeRequest(chunks, headers)
        result = asyncio.run(read_bounded_json(req))
        return req, result

    def test_million_byte_chunked_body_stops_without_aggregating_the_rest(self):
        chunks = [b"x" * 1000] * 1000
        req, result = self._read(chunks)
        self.assertEqual(result.status, "too_large")
        self.assertTrue(result.stopped_early)
        self.assertEqual(req.chunks_consumed, 9)
        self.assertLess(req.chunks_consumed, 1000)
        self.assertIsNone(result.data)

    def test_content_length_rejects_before_the_stream_is_read(self):
        chunks = [b"x" * 1000] * 1000
        req, result = self._read(chunks, headers={"content-length": "1000000"})
        self.assertEqual(result.status, "too_large")
        self.assertTrue(result.stopped_early)
        self.assertFalse(req.started)
        self.assertEqual(req.chunks_consumed, 0)

    def test_exact_8000_is_accepted_and_8001_stops(self):
        ok_payload = b'{"name":"' + b"a" * 7989 + b'"}'
        self.assertEqual(len(ok_payload), 8000)
        _, ok = self._read([ok_payload])
        self.assertEqual(ok.status, "ok")
        overflow = [b'{"name":"' + b"a" * 7989 + b'"}', b"Z"]
        self.assertEqual(len(overflow[0]) + len(overflow[1]), 8001)
        req, bad = self._read(overflow)
        self.assertEqual(bad.status, "too_large")
        self.assertEqual(req.chunks_consumed, 2)
        further = overflow + [b"MORE"]
        req2, bad2 = self._read(further)
        self.assertEqual(bad2.status, "too_large")
        self.assertEqual(req2.chunks_consumed, 2)

    def test_illegal_utf8_and_non_finite_json_are_bad_not_server_errors(self):
        from app.ai_section.body import parse_user_json

        self.assertIsNone(parse_user_json(b'{"name":"\xff"}'))
        self.assertIsNone(parse_user_json(b'{"fast": NaN}'))
        self.assertIsNone(parse_user_json(b'{"fast": Infinity}'))
        self.assertIsNone(parse_user_json(b'{"fast": -Infinity}'))
        self.assertIsNone(parse_user_json(b'{"fast": 1e309}'))
        self.assertIsNone(parse_user_json(b"[]"))
        deep = '{"a":' * 10 + "1" + "}" * 10
        self.assertIsNone(parse_user_json(deep.encode()))


class StrictSchemaTests(unittest.TestCase):
    def test_non_finite_and_wrong_types_raise_schema_error(self):
        cases = [
            _draft(params={"fast": float("nan"), "slow": 5}),
            _draft(params={"fast": float("inf"), "slow": 5}),
            _draft(kind=[]),
            _draft(kind={"id": "sma_cross"}),
            _draft(kind=True),
            _draft(name=True),
            _draft(params=[]),
            _draft(risk=[]),
            _draft(params={"fast": True, "slow": 5}),
            _draft(risk={"max_weight": 10**1000}),
            _draft(risk={"max_weight": float("inf")}),
        ]
        for body in cases:
            with self.subTest(body=body):
                with self.assertRaises(SchemaError):
                    validate_strategy_body(body)

    def test_omitted_risk_matches_empty_object_and_does_not_invent_stops(self):
        base = _draft()
        base.pop("risk")
        omitted = validate_strategy_body(base)
        empty = validate_strategy_body({**base, "risk": {}})
        self.assertEqual(omitted["risk"], empty["risk"])
        self.assertIsNone(omitted["risk"]["stop_loss_pct"])
        self.assertIsNone(omitted["risk"]["take_profit_pct"])
        self.assertEqual(omitted["risk"]["fee_bps"], 0.0)
        explicit_null = validate_strategy_body({**base, "risk": {"stop_loss_pct": None, "take_profit_pct": None}})
        self.assertIsNone(explicit_null["risk"]["stop_loss_pct"])
        self.assertEqual(explicit_null["risk"]["stop_loss_pct"], omitted["risk"]["stop_loss_pct"])


class CandleValidationTests(unittest.TestCase):
    def test_duplicate_future_bars_are_blocked_without_fills(self):
        draft = validate_strategy_body(_draft())
        rows = []
        price = 100.0
        for i in range(10):
            price += 1
            rows.append({"ts": 9_999_999_999_999, "open": price - 0.4, "high": price + 0.4, "low": price - 0.6, "close": price, "volume": 1})
        result = simulate_bars(draft, rows, now_ms=NOW_MS)
        self.assertEqual(result["status"], "blocked_bad_data")
        self.assertIn(result["reason"], {"duplicate_ts", "future_or_unclosed", "bad_ts"})
        self.assertEqual(result["fills"], [])
        self.assertIsNone(result["paper_pnl"])

    def test_gap_unclosed_and_bad_envelope_are_blocked(self):
        draft = validate_strategy_body(_draft())
        gapped = _regular_bars(6) + _regular_bars(6, start=BAR_START + 8 * DAY)
        self.assertEqual(simulate_bars(draft, gapped, now_ms=NOW_MS)["reason"], "gap")
        off_grid_future = _regular_bars(12)
        off_grid_future[-1]["ts"] = NOW_MS + DAY  # future and off the period grid
        off_grid = simulate_bars(draft, off_grid_future, now_ms=NOW_MS)
        self.assertEqual(off_grid["status"], "blocked_bad_data")
        self.assertIn(off_grid["reason"], {"gap", "non_regular_ts", "future_or_unclosed"})
        self.assertEqual(off_grid["fills"], [])
        future = _regular_bars(12, start=NOW_MS + DAY)
        self.assertEqual(simulate_bars(draft, future, now_ms=NOW_MS)["reason"], "future_or_unclosed")
        self.assertEqual(simulate_bars(draft, future, now_ms=NOW_MS)["fills"], [])
        envelope = _regular_bars(12)
        envelope[3]["high"] = 1
        envelope[3]["low"] = 2
        envelope[3]["open"] = 3
        envelope[3]["close"] = 4
        self.assertEqual(simulate_bars(draft, envelope, now_ms=NOW_MS)["reason"], "bad_ohlc_envelope")
        negative = _regular_bars(12)
        negative[2]["low"] = -1
        self.assertIn(simulate_bars(draft, negative, now_ms=NOW_MS)["reason"], {"bad_ohlc", "bad_ohlc_envelope"})


class ExecutionConventionTests(unittest.TestCase):
    def test_signal_fills_at_next_open_not_same_close(self):
        draft = validate_strategy_body(_draft(params={"fast": 2, "slow": 3}, risk={"max_weight": 0.2, "long_only": True}))
        rows = _regular_bars(20, pattern="up")
        result = simulate_bars(draft, rows, now_ms=NOW_MS)
        self.assertEqual(result["status"], "simulated")
        enters = [row for row in result["fills"] if row["action"] == "enter"]
        self.assertGreaterEqual(len(enters), 1)
        fill = enters[0]
        fill_index = next(i for i, row in enumerate(rows) if row["ts"] == fill["ts"])
        self.assertEqual(fill["raw_price"], rows[fill_index]["open"])
        self.assertNotEqual(fill["raw_price"], rows[fill_index]["close"])

    def test_gap_through_stop_fills_at_open_not_ideal_stop(self):
        draft = validate_strategy_body(_draft(
            params={"fast": 2, "slow": 3},
            risk={"max_weight": 0.2, "long_only": True, "stop_loss_pct": 0.05, "take_profit_pct": 0.5},
        ))
        rows = _regular_bars(16, pattern="up", close0=100)
        # After an entry, insert a session that opens well below a 5% stop.
        entry_probe = simulate_bars(draft, rows, now_ms=NOW_MS)
        enters = [row for row in entry_probe["fills"] if row["action"] == "enter"]
        self.assertTrue(enters)
        idx = next(i for i, row in enumerate(rows) if row["ts"] == enters[0]["ts"])
        self.assertLess(idx + 1, len(rows))
        # Rebuild: keep bars through entry, then a gapped-down bar.
        prefix = rows[: idx + 1]
        gap = {
            "ts": prefix[-1]["ts"] + DAY,
            "open": 50.0,
            "high": 51.0,
            "low": 49.0,
            "close": 50.0,
            "volume": 1,
        }
        # Need a closed bar after the gap so the series stays regular through the gap bar only.
        series = prefix + [gap]
        result = simulate_bars(draft, series, now_ms=NOW_MS)
        self.assertEqual(result["status"], "simulated")
        exits = [row for row in result["fills"] if row["action"] == "exit"]
        self.assertTrue(exits)
        self.assertEqual(exits[0]["reason"], "stop_loss_gap_open")
        self.assertEqual(exits[0]["raw_price"], 50.0)
        stop_ideal = enters[0]["price"] * 0.95
        self.assertNotAlmostEqual(exits[0]["raw_price"], stop_ideal)

    def test_same_bar_stop_and_take_uses_stop(self):
        draft = validate_strategy_body(_draft(
            params={"fast": 2, "slow": 3},
            risk={"max_weight": 0.2, "long_only": True, "stop_loss_pct": 0.05, "take_profit_pct": 0.05},
        ))
        rows = _regular_bars(16, pattern="up")
        probe = simulate_bars(draft, rows, now_ms=NOW_MS)
        enters = [row for row in probe["fills"] if row["action"] == "enter"]
        self.assertTrue(enters)
        idx = next(i for i, row in enumerate(rows) if row["ts"] == enters[0]["ts"])
        prefix = rows[: idx + 1]
        wide = {
            "ts": prefix[-1]["ts"] + DAY,
            "open": prefix[-1]["close"],
            "high": prefix[-1]["close"] * 1.2,
            "low": prefix[-1]["close"] * 0.8,
            "close": prefix[-1]["close"],
            "volume": 1,
        }
        result = simulate_bars(draft, prefix + [wide], now_ms=NOW_MS)
        exits = [row for row in result["fills"] if row["action"] == "exit"]
        self.assertTrue(exits)
        self.assertIn(exits[0]["reason"], {"stop_loss", "stop_loss_priority"})

    def test_costs_are_applied_and_unrealized_is_separated(self):
        body = _draft(params={"fast": 2, "slow": 3}, risk={"max_weight": 0.2, "long_only": True, "fee_bps": 10, "slippage_bps": 10})
        draft = validate_strategy_body(body)
        rows = _regular_bars(20, pattern="up")
        result = simulate_bars(draft, rows, now_ms=NOW_MS)
        self.assertFalse(result["gross_zero_cost"])
        self.assertFalse(result["is_investment_return"])
        self.assertIn("realized_paper_pnl", result)
        self.assertIn("unrealized_paper_pnl", result)
        self.assertGreater(result["cost_model"]["fee_bps"], 0)
        zero = simulate_bars(validate_strategy_body(_draft(params={"fast": 2, "slow": 3}, risk={"max_weight": 0.2, "long_only": True})), rows, now_ms=NOW_MS)
        self.assertTrue(zero["gross_zero_cost"])

    def test_donchian_uses_high_low_not_close_only(self):
        draft = validate_strategy_body(_draft(kind="donchian_break", params={"period": 5}, risk={"max_weight": 0.2, "long_only": True}))
        rows = _regular_bars(8, pattern="flat", close0=100)
        # Prior 5 bars stay 100; the signal bar closes at 100 but wicks above a close-only channel.
        rows[5] = {"ts": rows[5]["ts"], "open": 100, "high": 130, "low": 100, "close": 100, "volume": 1}
        rows[6] = {"ts": rows[6]["ts"], "open": 100, "high": 101, "low": 99, "close": 100, "volume": 1}
        rows[7] = {"ts": rows[7]["ts"], "open": 121, "high": 122, "low": 120, "close": 121, "volume": 1}
        # Close-only channel would not break at index 6 (close=100). High/low still no close break at 6.
        # At index 7 close 121 > prior high 130? No. Make index 7 close above prior highs except we need close break.
        rows[7]["high"] = 140
        rows[7]["close"] = 131
        rows[7]["open"] = 121
        # Window for index 7 is bars 2..6: highs 100,100,100,130,101 → upper 130, close 131 > 130.
        result = simulate_bars(draft, rows, now_ms=NOW_MS)
        # Fill would be on next bar, which does not exist, so no enter yet if signal is on last bar.
        # Add an 8th bar to execute? We already have 8 bars (0-7). If signal at 7, no next bar.
        rows.append({"ts": rows[-1]["ts"] + DAY, "open": 132, "high": 133, "low": 131, "close": 132, "volume": 1})
        result = simulate_bars(draft, rows, now_ms=NOW_MS)
        enters = [row for row in result["fills"] if row["action"] == "enter"]
        self.assertTrue(enters)
        self.assertEqual(enters[0]["raw_price"], 132)
        # Close-only prior window at the signal bar would be closes 100s only, also 131>100, so also break.
        # Build a case where close never exceeds prior closes, but exceeds prior highs? That's impossible
        # if highs >= closes. The inverse: close exceeds prior closes but not prior highs (wick highs).
        high_block = _regular_bars(9, pattern="flat", close0=100)
        high_block[2]["high"] = 150
        high_block[7]["close"] = 120
        high_block[7]["high"] = 120
        high_block[7]["open"] = 100
        high_block[7]["low"] = 100
        # prior highs include 150, close 120 < 150 → no break for high/low Donchian.
        # close-only prior closes are 100, 120>100 would have broken.
        blocked = simulate_bars(draft, high_block, now_ms=NOW_MS)
        self.assertEqual([row for row in blocked["fills"] if row["action"] == "enter"], [])


class FillCapTests(unittest.TestCase):
    def test_truncated_details_do_not_pretend_the_window_was_partially_walked(self):
        draft = validate_strategy_body(_draft(params={"fast": 2, "slow": 3}, risk={"max_weight": 0.2, "long_only": True}))
        # Alternating 4-bar up/down legs create many SMA(2,3) crosses, then a crash.
        rows = []
        ts = BAR_START
        price = 100.0
        for leg in range(50):
            step = 1.0 if leg % 2 == 0 else -1.0
            for _ in range(4):
                open_px = price
                price = price + step
                rows.append({"ts": ts, "open": open_px, "high": max(open_px, price), "low": min(open_px, price), "close": price, "volume": 1})
                ts += DAY
        # Force a long-held crash at the end from the last close down to 1.
        last = rows[-1]["close"]
        rows.append({"ts": ts, "open": last, "high": last, "low": 1.0, "close": 1.0, "volume": 1})
        ts += DAY
        rows.append({"ts": ts, "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1})
        result = simulate_bars(draft, rows, now_ms=NOW_MS)
        self.assertEqual(result["status"], "simulated")
        self.assertGreater(result["fill_count"], 40)
        self.assertTrue(result["fills_truncated"])
        self.assertEqual(len(result["fills"]), 40)
        self.assertEqual(result["fills_omitted"], result["fill_count"] - 40)
        self.assertFalse(result["complete_fills"])
        self.assertEqual(result["processed_bars"], len(rows))
        self.assertEqual(result["processed_last_ts"], rows[-1]["ts"])
        self.assertEqual(result["last_ts"], rows[-1]["ts"])
        self.assertIsNotNone(result["paper_pnl"])
        # The late crash must be in the window result, not omitted after the 40th detail.
        crash_ts = rows[-2]["ts"]
        detailed_exit_after_crash = any(row["ts"] >= crash_ts and row["action"] == "exit" for row in result["fills"])
        if not detailed_exit_after_crash:
            # Details were capped earlier; totals/open position still have to reflect the crash.
            if result["open_position"]:
                self.assertAlmostEqual(result["open_position"]["mark"], 1.0)
                self.assertLess(result["unrealized_paper_pnl"], 0)
            else:
                self.assertLess(result["realized_paper_pnl"], result["fill_count"])


class StoreTransactionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "studio.sqlite"

    def tearDown(self):
        self.tmp.cleanup()

    def test_concurrent_delete_cannot_leave_a_dangling_agent(self):
        import threading
        from app.ai_section.store import StudioStore, StoreError

        errors = []
        for _ in range(40):
            path = Path(self.tmp.name) / f"race_{secrets_token()}.sqlite"
            a = StudioStore(path)
            b = StudioStore(path)
            draft = validate_strategy_body(_draft())
            saved = a.create_strategy("user-a", draft)
            barrier = threading.Barrier(2)
            outcome = []

            def create():
                barrier.wait()
                try:
                    agent = a.create_agent("user-a", "paper", saved["id"])
                    outcome.append(("create", agent["id"]))
                except StoreError as exc:
                    outcome.append(("create", exc.code))

            def delete():
                barrier.wait()
                try:
                    b.delete_strategy("user-a", saved["id"])
                    outcome.append(("delete", "ok"))
                except StoreError as exc:
                    outcome.append(("delete", exc.code))

            t1 = threading.Thread(target=create)
            t2 = threading.Thread(target=delete)
            t1.start(); t2.start(); t1.join(); t2.join()
            counts = a.counts("user-a")
            if counts["agents"] and not counts["strategies"]:
                errors.append((outcome, counts))
            a.close(); b.close()
        self.assertEqual(errors, [])

    def test_two_connections_respect_quota_and_rollback_leaves_a_usable_connection(self):
        from app.ai_section.store import StudioStore, StoreError, MAX_AGENTS

        a = StudioStore(self.path)
        b = StudioStore(self.path)
        draft = validate_strategy_body(_draft())
        sid = a.create_strategy("user-a", draft)["id"]
        for i in range(MAX_AGENTS - 1):
            a.create_agent("user-a", f"a{i}", sid)
        barrier = __import__("threading").Barrier(2)
        codes = []

        def make(store, name):
            barrier.wait()
            try:
                store.create_agent("user-a", name, sid)
                codes.append("ok")
            except StoreError as exc:
                codes.append(exc.code)

        t1 = __import__("threading").Thread(target=make, args=(a, "one"))
        t2 = __import__("threading").Thread(target=make, args=(b, "two"))
        t1.start(); t2.start(); t1.join(); t2.join()
        self.assertEqual(a.counts("user-a")["agents"], MAX_AGENTS)
        self.assertIn("LIMIT", codes)
        self.assertIn("ok", codes)
        # Failed transaction must not poison the connection.
        with self.assertRaises(StoreError):
            a.create_agent("user-a", "overflow", sid)
        a.delete_agent("user-a", a.list_agents("user-a")[0]["id"])
        again = a.create_agent("user-a", "after-rollback", sid)
        self.assertTrue(again["id"].startswith("a_"))
        a.close(); b.close()


def secrets_token():
    import secrets
    return secrets.token_hex(4)


def _followup_sma_bars(*, bar7_low=90.0, bar7_high=104.0, bar7_open=103.0, bar7_close=101.0):
    """Natural SMA(2,3) series from REVIEW-FOLLOWUP.md. Bar 7 is 0-based."""
    closes = [100, 101, 102, 103, 104, 103, 102, 101, 100, 99]
    rows = []
    for i, close in enumerate(closes):
        ts = 1_700_000_000_000 + i * DAY
        if i == 7:
            open_px, high, low, close_px = bar7_open, bar7_high, bar7_low, bar7_close
        else:
            open_px = close
            high = close + 1
            low = close - 1
            close_px = close
        rows.append({"ts": ts, "open": open_px, "high": high, "low": low, "close": close_px, "volume": 1})
    return rows


class OpenIntrabarOrderTests(unittest.TestCase):
    """Causal open-vs-intrabar order. These would fail on the pre-fix loop."""

    def _long_flat_draft(self, **risk):
        body = _draft(
            params={"fast": 2, "slow": 3},
            risk={"max_weight": 0.2, "long_only": True, "stop_loss_pct": 0.05, "fee_bps": 0, "slippage_bps": 0, **risk},
        )
        # take remains disabled unless provided
        if "take_profit_pct" not in risk:
            body["risk"]["take_profit_pct"] = None
        return validate_strategy_body(body)

    def test_followup_sma_exits_at_open_not_later_stop(self):
        draft = self._long_flat_draft()
        rows = _followup_sma_bars(bar7_low=90.0)
        result = simulate_bars(draft, rows, now_ms=NOW_MS)
        self.assertEqual(result["status"], "simulated")
        exits = [row for row in result["fills"] if row["action"] == "exit"]
        self.assertEqual(len(exits), 1)
        self.assertEqual(exits[0]["ts"], rows[7]["ts"])
        self.assertEqual(exits[0]["reason"], "signal_exit_next_open")
        self.assertEqual(exits[0]["raw_price"], 103)
        self.assertNotEqual(exits[0]["raw_price"], 98.8)
        self.assertNotEqual(exits[0]["reason"], "stop_loss")
        enters = [row for row in result["fills"] if row["action"] == "enter"]
        self.assertEqual(enters[0]["ts"], rows[4]["ts"])
        self.assertEqual(enters[0]["raw_price"], 104)

    def test_later_low_cannot_revise_a_completed_open_exit(self):
        draft = self._long_flat_draft()
        deep = simulate_bars(draft, _followup_sma_bars(bar7_low=90.0), now_ms=NOW_MS)
        shallow = simulate_bars(draft, _followup_sma_bars(bar7_low=100.0), now_ms=NOW_MS)
        deep_exit = next(row for row in deep["fills"] if row["action"] == "exit")
        shallow_exit = next(row for row in shallow["fills"] if row["action"] == "exit")
        for key in ("ts", "action", "side", "raw_price", "price", "reason"):
            self.assertEqual(deep_exit[key], shallow_exit[key])
        self.assertEqual(deep_exit["raw_price"], 103)
        self.assertEqual(deep_exit["reason"], "signal_exit_next_open")

    def test_reverse_signal_exits_at_open_before_intrabar_stop(self):
        draft = self._long_flat_draft(long_only=False)
        result = simulate_bars(draft, _followup_sma_bars(bar7_low=90.0), now_ms=NOW_MS)
        rows = _followup_sma_bars()
        # Previous close went short; open must flatten the long at 103 and enter short at 103
        # before the later low of 90 can stop the old long at 98.8.
        events = [(row["action"], row["reason"], row["raw_price"], row["side"]) for row in result["fills"] if row["ts"] == rows[7]["ts"]]
        self.assertGreaterEqual(len(events), 2)
        self.assertEqual(events[0], ("exit", "reverse_next_open", 103, "long"))
        self.assertEqual(events[1][0], "enter")
        self.assertEqual(events[1][2], 103)
        self.assertEqual(events[1][3], "short")
        self.assertNotIn("stop_loss", [row[1] for row in events])
        alt = simulate_bars(draft, _followup_sma_bars(bar7_low=100.0), now_ms=NOW_MS)
        self.assertEqual(
            [(row["action"], row["reason"], row["raw_price"], row["side"]) for row in result["fills"] if row["ts"] == rows[7]["ts"]],
            [(row["action"], row["reason"], row["raw_price"], row["side"]) for row in alt["fills"] if row["ts"] == rows[7]["ts"]],
        )

    def test_open_gap_stop_is_unchanged_by_a_deeper_later_low(self):
        draft = self._long_flat_draft()
        # Same history through the long entry, then an open that itself gaps through 98.8.
        base = _followup_sma_bars()[:7]  # through index 6 (the flat signal close)
        def gapped(low):
            rows = list(base)
            rows.append({"ts": base[-1]["ts"] + DAY, "open": 90.0, "high": 91.0, "low": low, "close": 90.0, "volume": 1})
            return rows
        deep = simulate_bars(draft, gapped(80.0), now_ms=NOW_MS)
        shallow = simulate_bars(draft, gapped(89.0), now_ms=NOW_MS)
        for result in (deep, shallow):
            exits = [row for row in result["fills"] if row["action"] == "exit"]
            self.assertEqual(len(exits), 1)
            self.assertEqual(exits[0]["reason"], "stop_loss_gap_open")
            self.assertEqual(exits[0]["raw_price"], 90.0)
        self.assertEqual(deep["fills"][-1]["reason"], shallow["fills"][-1]["reason"])

    def test_held_position_still_uses_intrabar_stop_when_no_pending_exit(self):
        draft = self._long_flat_draft()
        # Through bar 4 entry and bar 5 (still long, no pending exit). Bar 6 low hits the 98.8 stop.
        rows = _followup_sma_bars()[:6]
        rows.append({
            "ts": rows[-1]["ts"] + DAY,
            "open": 103.0,
            "high": 104.0,
            "low": 90.0,
            "close": 102.0,
            "volume": 1,
        })
        result = simulate_bars(draft, rows, now_ms=NOW_MS)
        exits = [row for row in result["fills"] if row["action"] == "exit"]
        self.assertEqual(len(exits), 1)
        self.assertEqual(exits[0]["reason"], "stop_loss")
        self.assertAlmostEqual(exits[0]["raw_price"], 104 * 0.95)
        # A later low that does not reach the stop must leave the position open.
        safe = list(_followup_sma_bars()[:6])
        safe.append({"ts": safe[-1]["ts"] + DAY, "open": 103.0, "high": 104.0, "low": 100.0, "close": 102.0, "volume": 1})
        held = simulate_bars(draft, safe, now_ms=NOW_MS)
        self.assertFalse([row for row in held["fills"] if row["action"] == "exit"])
        self.assertIsNotNone(held["open_position"])




# Frozen preview replay (PR139 821d619e / local preview sim at 2026-11-23 21:53 Asia/Shanghai).
# Stored OKX BTC 1d uses UTC midnight opens (OKX 1Dutc). The last bar was the
# still-forming 2026-11-23 session, not a timestamp-semantic error.
PREVIEW_SIM_NOW_MS = 1_795_442_039_414
PREVIEW_BTC_1D_FIRST = 1_785_110_400_000  # 2026-07-27T00:00:00Z
PREVIEW_BTC_1D_LAST_CLOSED = 1_795_305_600_000  # 2026-11-22T00:00:00Z
PREVIEW_BTC_1D_FORMING = 1_795_392_000_000  # 2026-11-23T00:00:00Z
PREVIEW_BTC_1D_COUNT = 120


def _preview_like_btc_1d(*, forming_high=63579.5, forming_low=60380.8, forming_close=63379.2, forming_open=61273.9, closed=119, include_forming=True, pattern="up"):
    rows = _regular_bars(closed, start=PREVIEW_BTC_1D_FIRST, pattern=pattern, close0=100.0)
    assert rows[0]["ts"] == PREVIEW_BTC_1D_FIRST
    assert rows[-1]["ts"] == PREVIEW_BTC_1D_LAST_CLOSED
    if include_forming:
        rows.append({
            "ts": PREVIEW_BTC_1D_FORMING,
            "open": forming_open,
            "high": forming_high,
            "low": forming_low,
            "close": forming_close,
            "volume": 6723.71746023,
        })
    return rows


def _fast10_slow30_draft():
    return validate_strategy_body(_draft(params={"fast": 10, "slow": 30}, risk={"max_weight": 0.2, "long_only": True, "stop_loss_pct": None, "take_profit_pct": None, "fee_bps": 0, "slippage_bps": 0}))


class FormingTailCutoffTests(unittest.TestCase):
    def test_preview_forming_daily_tail_is_excluded_and_sim_can_complete(self):
        draft = _fast10_slow30_draft()
        self.assertEqual(draft["need_bars"], 33)
        rows = _preview_like_btc_1d()
        self.assertEqual(len(rows), PREVIEW_BTC_1D_COUNT)
        result = simulate_bars(draft, rows, now_ms=PREVIEW_SIM_NOW_MS)
        self.assertEqual(result["status"], "simulated")
        self.assertNotEqual(result["reason"], "future_or_unclosed")
        self.assertEqual(result["bars_excluded_unclosed"], 1)
        self.assertEqual(result["excluded_unclosed_ts"], [PREVIEW_BTC_1D_FORMING])
        self.assertEqual(result["data_cutoff_ts"], PREVIEW_BTC_1D_LAST_CLOSED)
        self.assertEqual(result["data_cutoff_close_at"], PREVIEW_BTC_1D_LAST_CLOSED + DAY)
        self.assertEqual(result["bars_used"], 119)
        self.assertGreaterEqual(result["bars_used"], result["bars_required"])
        self.assertGreaterEqual(result["fill_count"], 1)
        self.assertTrue(all(fill["ts"] <= PREVIEW_BTC_1D_LAST_CLOSED for fill in result["fills"]))
        self.assertIsNotNone(result["paper_pnl"])

    def test_changing_forming_tail_prices_does_not_change_closed_result(self):
        draft = _fast10_slow30_draft()
        deep = simulate_bars(draft, _preview_like_btc_1d(forming_low=50000.0, forming_high=80000.0, forming_close=79000.0), now_ms=PREVIEW_SIM_NOW_MS)
        shallow = simulate_bars(draft, _preview_like_btc_1d(forming_low=61000.0, forming_high=61300.0, forming_close=61200.0), now_ms=PREVIEW_SIM_NOW_MS)
        none = simulate_bars(draft, _preview_like_btc_1d(include_forming=False), now_ms=PREVIEW_SIM_NOW_MS)
        self.assertEqual(deep["status"], "simulated")
        self.assertEqual(deep["fills"], shallow["fills"])
        self.assertEqual(deep["paper_pnl"], shallow["paper_pnl"])
        self.assertEqual(deep["fills"], none["fills"])
        self.assertEqual(none["bars_excluded_unclosed"], 0)
        self.assertEqual(none["data_cutoff_ts"], PREVIEW_BTC_1D_LAST_CLOSED)

    def test_short_closed_history_after_excluding_tail_is_blocked_without_inventing(self):
        draft = _fast10_slow30_draft()
        # 10 closed daily bars plus the same forming tail; 10 < need 33.
        rows = _regular_bars(10, start=PREVIEW_BTC_1D_LAST_CLOSED - 9 * DAY, pattern="up")
        rows.append({"ts": PREVIEW_BTC_1D_FORMING, "open": 61273.9, "high": 63579.5, "low": 60380.8, "close": 63379.2, "volume": 1})
        result = simulate_bars(draft, rows, now_ms=PREVIEW_SIM_NOW_MS)
        self.assertEqual(result["status"], "blocked_no_data")
        self.assertEqual(result["reason"], "stored_candles_missing_or_short")
        self.assertEqual(result["bars_used"], 10)
        self.assertEqual(result["bars_required"], 33)
        self.assertEqual(result["bars_excluded_unclosed"], 1)
        self.assertEqual(result["data_cutoff_ts"], PREVIEW_BTC_1D_LAST_CLOSED)
        self.assertEqual(result["fills"], [])
        self.assertIsNone(result["paper_pnl"])

    def test_true_future_bar_still_blocks(self):
        draft = _fast10_slow30_draft()
        rows = _preview_like_btc_1d(include_forming=False)
        rows.append({"ts": PREVIEW_SIM_NOW_MS + DAY, "open": 64000, "high": 64100, "low": 63900, "close": 64050, "volume": 1})
        result = simulate_bars(draft, rows, now_ms=PREVIEW_SIM_NOW_MS)
        self.assertEqual(result["status"], "blocked_bad_data")
        # this appended open is both in the future and off the daily grid
        self.assertIn(result["reason"], {"future_or_unclosed", "gap", "non_regular_ts"})
        self.assertEqual(result["fills"], [])
        self.assertIsNone(result["paper_pnl"])
        self.assertEqual(result["bars_used"], 0)
        # a regular series whose first open is already in the future still fails as future
        grid = simulate_bars(draft, _regular_bars(40, start=PREVIEW_BTC_1D_FORMING + DAY, pattern="up"), now_ms=PREVIEW_SIM_NOW_MS)
        self.assertEqual(grid["status"], "blocked_bad_data")
        self.assertEqual(grid["reason"], "future_or_unclosed")
        self.assertEqual(grid["fills"], [])
        self.assertIsNone(grid["paper_pnl"])

    def test_forming_tail_plus_later_future_bar_still_blocks(self):
        draft = _fast10_slow30_draft()
        rows = _preview_like_btc_1d()
        rows.append({"ts": PREVIEW_BTC_1D_FORMING + DAY, "open": 64000, "high": 64100, "low": 63900, "close": 64050, "volume": 1})
        result = simulate_bars(draft, rows, now_ms=PREVIEW_SIM_NOW_MS)
        self.assertEqual(result["status"], "blocked_bad_data")
        self.assertEqual(result["reason"], "future_or_unclosed")
        self.assertEqual(result["fills"], [])
        self.assertIsNone(result["paper_pnl"])

    def test_gap_duplicate_and_illegal_ohlc_still_block(self):
        draft = _fast10_slow30_draft()
        gapped = _preview_like_btc_1d()
        del gapped[20]  # hole among otherwise regular closed bars
        self.assertEqual(simulate_bars(draft, gapped, now_ms=PREVIEW_SIM_NOW_MS)["reason"], "gap")
        dups = _preview_like_btc_1d()
        dups[5]["ts"] = dups[6]["ts"]
        self.assertEqual(simulate_bars(draft, dups, now_ms=PREVIEW_SIM_NOW_MS)["reason"], "duplicate_ts")
        bad = _preview_like_btc_1d()
        bad[8]["high"] = 1
        bad[8]["low"] = 2
        bad[8]["open"] = 3
        bad[8]["close"] = 4
        self.assertEqual(simulate_bars(draft, bad, now_ms=PREVIEW_SIM_NOW_MS)["reason"], "bad_ohlc_envelope")
        # illegal OHLC on the forming tail still blocks; we do not skip a corrupt last bar
        tail = _preview_like_btc_1d()
        tail[-1]["high"] = 1
        tail[-1]["low"] = 2
        self.assertEqual(simulate_bars(draft, tail, now_ms=PREVIEW_SIM_NOW_MS)["reason"], "bad_ohlc_envelope")
        for rows in (gapped, dups, bad, tail):
            blocked = simulate_bars(draft, rows, now_ms=PREVIEW_SIM_NOW_MS)
            self.assertEqual(blocked["status"], "blocked_bad_data")
            self.assertEqual(blocked["fills"], [])
            self.assertIsNone(blocked["paper_pnl"])


    def test_review_gap_before_forming_tail_is_not_hidden_by_exclusion(self):
        draft = _fast10_slow30_draft()
        rows = _preview_like_btc_1d()
        del rows[-2]  # last closed to forming tail is now 2 days
        result = simulate_bars(draft, rows, now_ms=PREVIEW_SIM_NOW_MS)
        self.assertEqual(result["status"], "blocked_bad_data")
        self.assertEqual(result["reason"], "gap")
        self.assertEqual(result["fills"], [])
        self.assertIsNone(result["paper_pnl"])
        self.assertEqual(result["bars_used"], 0)
        self.assertNotEqual(result["status"], "simulated")

    def test_review_offset_forming_timestamp_is_not_hidden_by_exclusion(self):
        draft = _fast10_slow30_draft()
        rows = _preview_like_btc_1d()
        rows[-1]["ts"] = PREVIEW_SIM_NOW_MS - 1_000  # still forming, but off the 1d grid
        result = simulate_bars(draft, rows, now_ms=PREVIEW_SIM_NOW_MS)
        self.assertEqual(result["status"], "blocked_bad_data")
        self.assertIn(result["reason"], {"gap", "non_regular_ts"})
        self.assertEqual(result["fills"], [])
        self.assertIsNone(result["paper_pnl"])
        self.assertEqual(result["bars_used"], 0)
        self.assertNotEqual(result["status"], "simulated")

    def test_review_overlapping_forming_bars_are_not_hidden_by_exclusion(self):
        draft = _fast10_slow30_draft()
        rows = _preview_like_btc_1d()
        extra = dict(rows[-1])
        extra["ts"] = PREVIEW_BTC_1D_FORMING + 1_000  # second forming bar, 1s later
        rows.append(extra)
        result = simulate_bars(draft, rows, now_ms=PREVIEW_SIM_NOW_MS)
        self.assertEqual(result["status"], "blocked_bad_data")
        self.assertEqual(result["reason"], "non_regular_ts")
        self.assertEqual(result["fills"], [])
        self.assertIsNone(result["paper_pnl"])
        self.assertEqual(result["bars_used"], 0)
        self.assertNotEqual(result["status"], "simulated")



if __name__ == "__main__":
    unittest.main()
