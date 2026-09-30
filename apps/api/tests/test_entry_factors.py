"""Holder-structure / token-safety entry factors (record only) and replay filters.

No network: the RPC is a fake callable; the real client is off under tests.
"""
from __future__ import annotations

import base64
import io
import json
import os
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from app.paper import entry_factors as ef
from app.paper import evidence as ev
from app.paper import holders as h
from app.paper import replay
from app.providers.pump_verify import bonding_curve_address
from app.providers.pumpfun_decode import b58encode
from app.providers.pumpfun_live_paper import PumpfunLivePaperProvider
from app.wallet.codec import b58decode

SUPPLY = 1_000_000_000_000_000  # 1e9 tokens, 6 decimals
PCT = SUPPLY // 100


def _key(n: int) -> str:
    return b58encode(bytes([n]) * 32)


MINT = _key(7)
DEV = _key(1)
B1, B2, S1, LATE, SELLER = _key(2), _key(3), _key(4), _key(5), _key(6)
CURVE = bonding_curve_address(MINT)


def _events():
    """Create slot 100. (ts, slot, trader, signed tokens)."""
    return [
        (1_000, 100, DEV, 5 * PCT),       # dev buy in the Create tx
        (1_000, 100, B1, 3 * PCT),        # bundle slot 0
        (1_400, 101, B2, 2 * PCT),        # bundle slot 1
        (3_000, 105, S1, 1 * PCT),        # sniper (first buy within 10 slots)
        (9_000, 120, LATE, 4 * PCT),      # regular buyer
        (9_500, 121, B1, -int(1.5 * PCT)),  # B1 sells half
        (9_600, 121, SELLER, -PCT // 10),   # sells tokens it got by transfer
        (20_000, 150, LATE, 10 * PCT),    # after the signal
    ]


def _snapshot(upto=10_000, **kw):
    book = h.HolderLedgerBook()
    for ts, slot, who, amt in _events():
        book.apply(MINT, trader=who, side="buy" if amt > 0 else "sell", token_amount=abs(amt), slot=slot, ts=ts)
    return book.snapshot(MINT, upto_ts=upto)


class TapeFactorTests(unittest.TestCase):
    def test_holdings_bundle_and_snipers_at_signal(self):
        out = h.tape_holder_factors(_snapshot(), creator=DEV, created_slot=100, from_create=True, supply=SUPPLY)
        self.assertTrue(out["complete"])
        self.assertEqual(out["events"], 7, "prints after the signal are cut")
        self.assertEqual(out["holder_count"], 5)  # DEV B1 B2 S1 LATE
        self.assertEqual(out["negative_wallets"], 1)
        self.assertAlmostEqual(out["dev_pct"], 5.0)
        self.assertAlmostEqual(out["top10_pct"], 5 + 1.5 + 2 + 1 + 4)
        self.assertEqual(out["bundle_wallets"], 2)
        self.assertAlmostEqual(out["bundle_pct"], 5.0)
        self.assertAlmostEqual(out["bundle_pct_incl_dev"], 10.0)
        self.assertAlmostEqual(out["bundle_held_pct"], 3.5)
        self.assertEqual(out["sniper_wallets"], 3)
        self.assertAlmostEqual(out["sniper_pct"], 6.0)
        self.assertAlmostEqual(out["sniper_held_pct"], 4.5)
        self.assertEqual(out["last_slot"], 121)
        self.assertEqual(out["null_reasons"], {})

    def test_top10_takes_only_ten_largest(self):
        book = h.HolderLedgerBook()
        for i in range(15):
            book.apply(MINT, trader=f"w{i}", side="buy", token_amount=(i + 1) * PCT // 10, slot=100 + i, ts=i)
        out = h.tape_holder_factors(book.snapshot(MINT), creator=None, created_slot=100, from_create=True, supply=SUPPLY)
        self.assertAlmostEqual(out["top10_pct"], sum(range(6, 16)) / 10)
        self.assertEqual(out["holder_count"], 15)
        self.assertIsNone(out["dev_pct"])
        self.assertEqual(out["null_reasons"]["dev_pct"], "creator_unknown")

    def test_null_reasons_instead_of_guesses(self):
        cases = {
            "no_ledger_for_mint": dict(snapshot=None, from_create=True),
            "ledger_not_from_create_event": dict(snapshot=_snapshot(), from_create=False),
        }
        for reason, kw in cases.items():
            out = h.tape_holder_factors(kw["snapshot"], creator=DEV, created_slot=100, from_create=kw["from_create"])
            self.assertFalse(out["complete"])
            self.assertIsNone(out["top10_pct"])
            self.assertEqual(out["null_reasons"]["top10_pct"], reason)
        out = h.tape_holder_factors(_snapshot(), creator=DEV, created_slot=None, from_create=True, supply=SUPPLY)
        self.assertIsNotNone(out["top10_pct"])
        self.assertIsNone(out["bundle_pct"])
        self.assertEqual(out["null_reasons"]["bundle_pct"], "no_create_slot")
        book = h.HolderLedgerBook()
        book.apply(MINT, trader=DEV, side="buy", token_amount=PCT, slot=None, ts=1)
        out = h.tape_holder_factors(book.snapshot(MINT), creator=DEV, created_slot=100, from_create=True, supply=SUPPLY)
        self.assertEqual(out["null_reasons"]["sniper_pct"], "no_slot_on_prints")

    def test_ledger_is_bounded(self):
        book = h.HolderLedgerBook(max_mints=2, max_events=3)
        for i in range(5):
            book.apply("m1", trader="w", side="buy", token_amount=1, slot=1, ts=i)
        book.apply("m1", trader=None, side="buy", token_amount=1, slot=1, ts=9)
        snap = book.snapshot("m1")
        self.assertEqual(len(snap["events"]), 3)
        self.assertTrue(snap["overflow"])
        self.assertEqual(snap["no_trader"], 1)
        out = h.tape_holder_factors(snap, creator="w", created_slot=1, from_create=True)
        self.assertEqual(out["null_reasons"]["top10_pct"], "ledger_overflow")
        book.apply("m2", trader="w", side="buy", token_amount=1, slot=1, ts=0)
        book.apply("m3", trader="w", side="buy", token_amount=1, slot=1, ts=0)
        self.assertEqual(len(book), 2)
        self.assertIsNone(book.snapshot("m1"), "oldest mint evicted")
        book.drop("m3")
        self.assertIsNone(book.snapshot("m3"))


def _mint_value(mint_auth=None, freeze_auth=None, program=h.TOKEN_2022_PROGRAM, supply=SUPPLY):
    return {
        "owner": program,
        "data": {"program": "spl-token-2022", "parsed": {"type": "mint", "info": {
            "decimals": 6, "supply": str(supply), "mintAuthority": mint_auth, "freezeAuthority": freeze_auth,
            "isInitialized": True}}},
    }


def _acct(owner: str, amount: int) -> dict:
    raw = b58decode(owner) + int(amount).to_bytes(8, "little")
    return {"pubkey": _key(99), "account": {"data": [base64.b64encode(raw).decode(), "base64"]}}


class RpcFactorTests(unittest.TestCase):
    def test_parse_mint_account(self):
        ok = h.parse_mint_account(_mint_value())
        self.assertTrue(ok["ok"] and ok["mint_authority_revoked"] and ok["freeze_authority_revoked"])
        self.assertTrue(ok["token_2022"])
        self.assertEqual(ok["supply"], SUPPLY)
        bad = h.parse_mint_account(_mint_value(mint_auth=DEV, freeze_auth=B1, program=h.SPL_TOKEN_PROGRAM))
        self.assertFalse(bad["mint_authority_revoked"])
        self.assertFalse(bad["freeze_authority_revoked"])
        self.assertEqual(bad["freeze_authority"], B1)
        self.assertEqual(h.parse_mint_account(None)["reason"], "mint_account_missing")
        self.assertEqual(h.parse_mint_account({"data": ["AAAA", "base64"]})["reason"], "mint_account_not_parsed")

    def test_gpa_params(self):
        p22 = h.token_accounts_gpa_params(MINT, h.TOKEN_2022_PROGRAM)
        self.assertEqual(p22[0], h.TOKEN_2022_PROGRAM)
        self.assertEqual(p22[1]["filters"], [{"memcmp": {"offset": 0, "bytes": MINT}}])
        self.assertEqual(p22[1]["dataSlice"], {"offset": 32, "length": 40})
        legacy = h.token_accounts_gpa_params(MINT, h.SPL_TOKEN_PROGRAM)
        self.assertEqual(legacy[1]["filters"][0], {"dataSize": 165})

    def test_holder_stats_exclude_curve_and_aggregate_owner(self):
        rows = h.decode_owner_amount_rows(
            [_acct(CURVE, 80 * PCT), _acct(DEV, 4 * PCT), _acct(B1, 3 * PCT), _acct(B1, 1 * PCT),
             _acct(S1, 0), _acct(LATE, 2 * PCT), {"account": {"data": ["", "base64"]}}]
        )
        self.assertEqual(len(rows), 6)
        st = h.holder_stats_from_accounts(rows, supply=SUPPLY, curve_owner=CURVE, creator=DEV)
        self.assertEqual(st["token_accounts"], 6)
        self.assertEqual(st["holder_count"], 3, "zero balances and the curve are not holders")
        self.assertAlmostEqual(st["curve_pct"], 80.0)
        self.assertAlmostEqual(st["top10_pct"], 10.0)
        self.assertAlmostEqual(st["top10_pct_circulating"], 50.0)
        self.assertAlmostEqual(st["top1_pct"], 4.0)
        self.assertAlmostEqual(st["dev_pct"], 4.0)
        st2 = h.holder_stats_from_accounts(rows, supply=SUPPLY, curve_owner=CURVE, creator=SELLER)
        self.assertEqual(st2["dev_pct"], 0.0, "dev with no account holds 0 %")


class FakeRpc:
    def __init__(self, *, delay=0.0, fail=None):
        self.calls = []
        self.delay = delay
        self.fail = fail
        self.lock = threading.Lock()

    def __call__(self, method, params):
        with self.lock:
            self.calls.append(method)
        if self.delay:
            time.sleep(self.delay)
        if self.fail:
            raise self.fail
        if method == "getAccountInfo":
            return {"context": {"slot": 130}, "value": _mint_value()}
        if method == "getProgramAccounts":
            return {"context": {"slot": 131}, "value": [_acct(CURVE, 80 * PCT), _acct(DEV, 4 * PCT), _acct(B1, 6 * PCT)]}
        raise AssertionError(method)


class _Provider:
    """curve_evidence + holder_ledger_snapshot, like the live provider."""

    def __init__(self, source="logs"):
        self.source = source

    def curve_evidence(self, symbol):
        return {"creator": DEV, "source": self.source, "created_slot": 100, "token_total_supply": SUPPLY}

    def holder_ledger_snapshot(self, mint, upto_ts=None):
        return _snapshot(upto=upto_ts)


class ServiceTests(unittest.TestCase):
    def _svc(self, rpc, **kw):
        kw.setdefault("clock", lambda: 10_050)
        svc = ef.EntryFactorService(rpc_call=rpc, rps=1000, timeout_s=1.0, **kw)
        self.addCleanup(svc.shutdown)
        return svc

    def test_submit_is_non_blocking_and_result_combines_sources(self):
        rpc = FakeRpc(delay=0.2)
        svc = self._svc(rpc)
        t0 = time.perf_counter()
        svc.submit(_Provider(), symbol="X/SOL", mint=MINT, signal_ts=10_000)
        self.assertLess(time.perf_counter() - t0, 0.05, "submit must not wait for the RPC")
        self.assertEqual(svc.result(MINT, 10_000)["status"], "pending")
        self.assertTrue(svc.wait_idle(5))
        r = svc.result(MINT, 10_000)
        self.assertEqual(r["status"], "ok")
        self.assertAlmostEqual(r["dev_pct"], 4.0)
        self.assertAlmostEqual(r["top10_pct"], 10.0)
        self.assertEqual(r["holder_count"], 2)
        self.assertEqual(r["sources"]["top10_pct"], "rpc")
        self.assertAlmostEqual(r["bundle_pct"], 5.0)
        self.assertAlmostEqual(r["sniper_pct"], 6.0)
        self.assertEqual(r["sources"]["bundle_pct"], "tape")
        self.assertIs(r["mint_authority_revoked"], True)
        self.assertIs(r["freeze_authority_revoked"], True)
        self.assertIsNone(r["insider_pct"])
        self.assertIn("insider_pct", r["null_reasons"])
        self.assertAlmostEqual(r["tape"]["top10_pct"], 13.5)
        self.assertEqual(r["rpc"]["slot"], 131)
        self.assertIn("done_after_signal_ms", r["lookup"])
        self.assertLess(svc.status()["submit_ns_max"], 50_000_000)

    def test_cache_per_mint(self):
        rpc = FakeRpc()
        svc = self._svc(rpc)
        svc.submit(_Provider(), symbol="X/SOL", mint=MINT, signal_ts=10_000)
        svc.wait_idle(5)
        svc.submit(_Provider(), symbol="X/SOL", mint=MINT, signal_ts=10_000)  # same signal: no new job
        svc.submit(_Provider(), symbol="X/SOL", mint=MINT, signal_ts=11_000)
        svc.wait_idle(5)
        self.assertEqual(rpc.calls, ["getAccountInfo", "getProgramAccounts"])
        self.assertEqual(svc.result(MINT, 11_000)["status"], "ok")
        self.assertGreaterEqual(svc.status()["cache_hits"], 2)

    def test_rpc_failure_falls_back_to_tape_with_reasons(self):
        svc = self._svc(FakeRpc(fail=ef.RpcRateLimited("429")))
        svc.submit(_Provider(), symbol="X/SOL", mint=MINT, signal_ts=10_000)
        svc.wait_idle(5)
        r = svc.result(MINT, 10_000)
        self.assertEqual(r["status"], "partial")
        self.assertAlmostEqual(r["top10_pct"], 13.5)
        self.assertEqual(r["sources"]["top10_pct"], "tape")
        self.assertIsNone(r["mint_authority_revoked"])
        self.assertEqual(r["null_reasons"]["mint_authority_revoked"], "rpc_rate_limited")

    def test_everything_null_with_reasons_when_no_source(self):
        svc = self._svc(None, rpc_enabled=False)
        svc.submit(_Provider(source="pumpportal"), symbol="X/SOL", mint=MINT, signal_ts=10_000)
        svc.wait_idle(5)
        r = svc.result(MINT, 10_000)
        for k in ef.FACTOR_KEYS:
            self.assertIsNone(r[k], k)
            self.assertTrue(r["null_reasons"][k], k)
        self.assertEqual(r["null_reasons"]["mint_authority_revoked"], "rpc_disabled")
        self.assertEqual(r["null_reasons"]["top10_pct"], "rpc_disabled")

    def test_rpc_off_by_default_under_tests(self):
        svc = ef.EntryFactorService()
        self.addCleanup(svc.shutdown)
        self.assertFalse(svc.rpc_enabled)

    def test_busy_queue_drops_with_reason(self):
        gate = threading.Event()

        class Slow(_Provider):
            def curve_evidence(self, symbol):
                gate.wait(5)
                return super().curve_evidence(symbol)

        svc = self._svc(FakeRpc(), workers=1)
        with mock.patch.object(ef, "MAX_INFLIGHT", 2):
            for i in range(3):
                svc.submit(Slow(), symbol="X/SOL", mint=MINT, signal_ts=10_000 + i)
            self.assertEqual(svc.result(MINT, 10_002)["status"], "dropped_busy")
            gate.set()
            svc.wait_idle(5)
        self.assertEqual(svc.result(MINT, 10_000)["status"], "ok")
        self.assertEqual(svc.status()["dropped_busy"], 1)

    def test_lookup_errors_stay_in_worker(self):
        class Broken(_Provider):
            def curve_evidence(self, symbol):
                raise RuntimeError("boom")

        svc = self._svc(FakeRpc())
        with self.assertLogs("auu.paper.entry_factors", "ERROR"):
            svc.submit(Broken(), symbol="X/SOL", mint=MINT, signal_ts=1)
            svc.wait_idle(5)
        self.assertEqual(svc.result(MINT, 1)["status"], "error:RuntimeError")
        self.assertEqual(svc.result(MINT, 999)["status"], "not_submitted")


class ProviderLedgerTests(unittest.TestCase):
    def test_logs_prints_feed_ledger_with_slots(self):
        p = PumpfunLivePaperProvider(watch_mints="")
        p.register_watch_mint(MINT, base="HOLD", source="logs", creator=DEV)
        p.note_create_slot(MINT, 100)
        p.note_create_slot(MINT, 999)  # first slot wins
        base = {"mint": MINT, "virtual_sol_reserves": 31_000_000_000, "virtual_token_reserves": 1_040_000_000_000_000,
                "real_sol_reserves": 1_000_000_000, "real_token_reserves": 760_000_000_000_000, "sol_amount": 1_000_000_000,
                "timestamp": 1}
        with mock.patch("app.providers.pumpfun_live_paper.extract_trades_from_logs",
                        return_value=[dict(base, is_buy=True, token_amount=5 * PCT, user=DEV)]):
            row = p.observe_logs(["x"], signature="s1", recv_ts=1_000, slot=100)
        self.assertEqual(row["slot"], 100)
        p.observe_trade_event(dict(base, is_buy=True, token_amount=2 * PCT, user=B1, slot=101, signature="s2"), recv_ts=1_500)
        info = p.curve_evidence("HOLD/SOL")
        self.assertEqual(info["created_slot"], 100)
        snap = p.holder_ledger_snapshot(MINT, upto_ts=2_000)
        self.assertEqual([e[1:] for e in snap["events"]], [(100, DEV, 5 * PCT), (101, B1, 2 * PCT)])
        out = h.tape_holder_factors(snap, creator=DEV, created_slot=info["created_slot"], from_create=True)
        self.assertAlmostEqual(out["dev_pct"], 5.0)
        self.assertAlmostEqual(out["bundle_pct"], 2.0)
        p._drop_locked("HOLD/SOL", MINT)
        self.assertIsNone(p.holder_ledger_snapshot(MINT))

    def test_discovery_forwards_slot(self):
        from app.discovery import DiscoveryRuntime

        seen = {}

        class Prov:
            def observe_logs(self, logs, **kw):
                seen.update(kw)

        rt = DiscoveryRuntime()
        with mock.patch("app.discovery.get_provider", return_value=Prov()):
            rt._forward_trade_logs(["l"], "sig", 4242)
            self.assertEqual(seen, {"signature": "sig", "slot": 4242})
            seen.clear()
            rt._forward_trade_logs(["l"], "sig")
            self.assertEqual(seen, {"signature": "sig"})


class ReplayFilterTests(unittest.TestCase):
    def test_parse_and_match(self):
        self.assertEqual(replay.parse_expr("top10_pct>=30"), ("top10_pct", ">=", 30.0))
        self.assertEqual(replay.parse_expr("mint_authority_revoked==true"), ("mint_authority_revoked", "==", True))
        with self.assertRaises(ValueError):
            replay.parse_expr("top10_pct~3")
        f = {"top10_pct": 35.0, "dev_pct": None, "mint_authority_revoked": True, "rpc": {"curve_pct": 80.0}}
        self.assertTrue(replay.match_expr(f, replay.parse_expr("top10_pct>30")))
        self.assertFalse(replay.match_expr(f, replay.parse_expr("top10_pct<=30")))
        self.assertIsNone(replay.match_expr(f, replay.parse_expr("dev_pct>5")))
        self.assertTrue(replay.match_expr(f, replay.parse_expr("dev_pct==null")))
        self.assertTrue(replay.match_expr(f, replay.parse_expr("mint_authority_revoked==true")))
        self.assertTrue(replay.match_expr(f, replay.parse_expr("rpc.curve_pct>=80")))
        self.assertIsNone(replay.match_expr(None, replay.parse_expr("top10_pct>1")))

    def test_bucket_labels(self):
        edges = [5.0, 10.0]
        self.assertEqual(replay.bucket_label(1, edges), "<5")
        self.assertEqual(replay.bucket_label(5, edges), "[5,10)")
        self.assertEqual(replay.bucket_label(12, edges), ">=10")
        self.assertEqual(replay.bucket_label(None, edges), "null")
        self.assertEqual(replay.bucket_label(True, edges), "true")

    def test_filter_split_bucket(self):
        rows = [
            {"status": "ok", "net_bps": 100.0, "net_sol": 0.001, "factors": {"top10_pct": 10.0}},
            {"status": "ok", "net_bps": 300.0, "net_sol": 0.003, "factors": {"top10_pct": 20.0}},
            {"status": "ok", "net_bps": -500.0, "net_sol": -0.005, "factors": {"top10_pct": 60.0}},
            {"status": "ok", "net_bps": -100.0, "net_sol": -0.001, "factors": {"top10_pct": None}},
        ]
        w = [replay.parse_expr("top10_pct<50")]
        self.assertEqual(len(replay.filter_results(rows, w)), 2)
        self.assertEqual(len(replay.filter_results(rows, w, keep_null=True)), 3)
        sp = replay.split_report(rows, replay.parse_expr("top10_pct>=50"))
        self.assertEqual((sp["match"]["n"], sp["no_match"]["n"], sp["null"]["n"]), (1, 2, 1))
        self.assertAlmostEqual(sp["no_match"]["mean_net_bps"], 200.0)
        self.assertAlmostEqual(sp["no_match"]["win_rate"], 1.0)
        b = replay.bucket_report(rows, "top10_pct", [15.0, 50.0])
        self.assertEqual(list(b), ["<15", "[15,50)", ">=50", "null"])
        self.assertAlmostEqual(b[">=50"]["mean_net_bps"], -500.0)


class EvidenceIntegrationTests(unittest.IsolatedAsyncioTestCase):
    """A real engine trade: the record carries entry_factors; decisions unchanged."""

    async def asyncSetUp(self):
        from tests import test_paper_evidence as tpe

        self.tpe = tpe
        self.case = tpe.EngineEvidenceTests("test_full_trade_is_recorded")
        self.case.setUp()
        self.addCleanup(self.case.tearDown)
        self.rpc = FakeRpc()
        self.svc = ef.EntryFactorService(rpc_call=self.rpc, rps=1000, timeout_s=1.0, clock=lambda: tpe.T0 + 100)
        ef.set_entry_factor_service(self.svc)
        self.addCleanup(ef.set_entry_factor_service, None)

    async def test_record_has_factors_and_hook_timing(self):
        from app.strategies.pump_paper_v1 import PumpPaperEngine

        orig = PumpPaperEngine._poll_evidence
        svc = self.svc

        def poll(engine, provider, now_ms):
            svc.wait_idle(5)
            return orig(engine, provider, now_ms)

        with mock.patch.object(PumpPaperEngine, "_poll_evidence", poll), mock.patch.object(self.tpe, "CREATOR", DEV):
            await self.case._run_trade()
        recs = self.case._records()
        self.assertEqual(len(recs), 1)
        r = recs[0]
        self.assertEqual(r["v"], 3)
        f = r["entry_factors"]
        self.assertEqual(f["signal_ts"], r["entry"]["signal_ts"])
        self.assertIn(f["status"], {"ok", "partial"})
        self.assertAlmostEqual(f["dev_pct"], 4.0, msg=json.dumps({k: f[k] for k in ("sources", "rpc", "tape")}, default=str)[:1500])
        self.assertIs(f["mint_authority_revoked"], True)
        # The test tape has no slots and no Create slot → bundle null with a reason.
        self.assertIsNone(f["bundle_pct"])
        self.assertEqual(f["null_reasons"]["bundle_pct"], "no_create_slot")
        self.assertTrue(f["tape"]["complete"])
        self.assertIn("evidence_hook_us", r["entry"])
        self.assertIn("factor_submit_us", r["entry"])
        self.assertEqual(self.rpc.calls, ["getAccountInfo", "getProgramAccounts"])

    async def test_factors_off_keeps_record(self):
        with mock.patch.dict(os.environ, {"AUU_ENTRY_FACTORS": "off"}):
            await self.case._run_trade()
        r = self.case._records()[0]
        self.assertEqual(r["entry_factors"]["status"], "disabled")
        self.assertEqual(self.rpc.calls, [])

    async def test_replay_cli_filters_on_factors(self):
        from app.strategies.pump_paper_v1 import PumpPaperEngine

        orig = PumpPaperEngine._poll_evidence
        svc = self.svc

        def poll(engine, provider, now_ms):
            svc.wait_idle(5)
            return orig(engine, provider, now_ms)

        with mock.patch.object(PumpPaperEngine, "_poll_evidence", poll), mock.patch.object(self.tpe, "CREATOR", DEV):
            await self.case._run_trade()
        store = str(self.case.store)
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = replay.main(["--evidence", store, "--json", "--where", "dev_pct<=5",
                              "--split", "top10_pct>95", "--bucket", "dev_pct:1,5,10"])
        self.assertEqual(rc, 0)
        s = json.loads(buf.getvalue())["summary"]
        self.assertEqual((s["n_before_filter"], s["n_records"]), (1, 1))
        self.assertEqual(s["splits"]["top10_pct>95"]["no_match"]["n"], 1)
        self.assertEqual(s["buckets"]["dev_pct:1,5,10"]["[1,5)"]["n"], 1)
        buf = io.StringIO()
        with redirect_stdout(buf):
            replay.main(["--evidence", store, "--json", "--where", "dev_pct>5"])
        self.assertEqual(json.loads(buf.getvalue())["summary"]["n_records"], 0)
        buf = io.StringIO()
        with redirect_stdout(buf):
            replay.main(["--evidence", store, "--split", "mint_authority_revoked==true"])
        self.assertIn("split mint_authority_revoked==true", buf.getvalue())
        with redirect_stdout(io.StringIO()), mock.patch("sys.stderr", new=io.StringIO()):
            self.assertEqual(replay.main(["--evidence", store, "--where", "bad"]), 2)


if __name__ == "__main__":
    unittest.main()
