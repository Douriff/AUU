"""Real-market paper (DATA_PROVIDER=pumpfun_live_paper).

Covers: TradeEvent decode (synthetic + real mainnet vector), PDA / bonding-curve
verification with a mocked RPC, prices that only move on real prints, PumpPortal
unit handling and subscription with a fake websocket, synthetic / mock / legacy
exclusion from stats, curve-quote fills with protocol fee and latency, full-qty
sells, the residual-short fix, the legacy cleanup script, and shadow protection.

No test opens a socket or calls an HTTP endpoint: network entry points are
patched to raise, and every store lives in the per-process temp data dir.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import os
import struct
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.discovery import DiscoveryRuntime, discovery_accepts, normalize_new_token
from app.models.contracts import (
    AccountCtx,
    Fill,
    LiquidityCtx,
    OrderIntent,
    PumpCtx,
    PumpfunPaperSnapshot,
    SignalOut,
    StrategyContext,
    TickCtx,
)
from app.paper.broker import PaperBroker, get_paper_broker, reset_paper_broker
from app.paper.ledger import (
    RoundTrip,
    _load_journal,
    build_performance,
    excluded_from_go,
    get_paper_journal,
    reset_paper_ledger,
)
from app.paper.legacy_cleanup import clean_journal, main as cleanup_main, run as cleanup_run
from app.paper.real_fill import curve_fee_bps, quote_curve_fill
from app.paper.shadow_compare import (
    _Open,
    _opens,
    apply_shadow_config,
    build_shadow_compare,
    observe_candidate,
    reset_shadow_compare,
    shadow_closed,
)
from app.providers.pump_verify import (
    bonding_curve_address,
    find_program_address,
    verify_pump_mint,
)
from app.providers.pumpfun_curve_math import (
    INITIAL_REAL_TOKEN_RESERVES,
    INITIAL_VIRTUAL_SOL_RESERVES,
    INITIAL_VIRTUAL_TOKEN_RESERVES,
    LAMPORTS_PER_SOL,
    buy_tokens_out,
    price_sol,
    sell_sol_out,
    sol_after_buy_fee,
)
from app.providers.pumpfun_decode import (
    GLOBAL_PDA,
    PUMP_PROGRAM_ID,
    decode_trade_event,
    extract_trade_from_logs,
    extract_trades_from_logs,
    trade_event_discriminator,
)
from app.providers.pumpfun_live_paper import PumpfunLivePaperProvider, sol_to_lamports, tokens_to_raw
from app.providers.pumpfun_paper import PumpfunPaperProvider
from app.risk.gate import reset_risk_gate
from app.strategies.pump_paper_v1 import PumpPaperEngine, PumpPaperParams, TapeWindow, reset_engine

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "pump_live"
REAL_MINT = "AH3HLZMUCRkjytVDuJbbnD11sJ1F86R3LkZ71H2Zpump"
INIT = {
    "virtual_sol": INITIAL_VIRTUAL_SOL_RESERVES,
    "virtual_token": INITIAL_VIRTUAL_TOKEN_RESERVES,
    "real_sol": 0,
    "real_token": INITIAL_REAL_TOKEN_RESERVES,
}


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _no_network():
    """Patch every outbound entry point this feature could use to fail loudly."""

    def boom(*_a, **_k):
        raise AssertionError("network access in a unit test")

    return [
        mock.patch("httpx.post", side_effect=boom),
        mock.patch("httpx.get", side_effect=boom),
        mock.patch("websockets.connect", side_effect=boom),
    ]


class NoNetworkCase(unittest.TestCase):
    def setUp(self):
        self._patches = _no_network()
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()


def _ok_verifier(reserves: dict | None = None):
    def _v(mint: str):
        return {"ok": True, "retry": False, "reason": "pump_curve", "reserves": reserves or dict(INIT)}

    return _v


def _live(verifier=None, clock=None) -> PumpfunLivePaperProvider:
    return PumpfunLivePaperProvider(watch_mints="", verifier=verifier or _ok_verifier(), clock=clock)


def _pack_trade(
    *,
    sol_amount: int = 50_000_000,
    token_amount: int = 1_000_000_000_000,
    is_buy: bool = True,
    timestamp: int = 1_700_000_000,
    virtual_sol: int = 30_050_000_000,
    virtual_token: int = 1_072_000_000_000_000,
    real_sol: int = 50_000_000,
    real_token: int = 792_100_000_000_000,
    fee_bps: int = 100,
    fee: int = 500_000,
    trailing: bytes = b"",
) -> bytes:
    return b"".join(
        [
            trade_event_discriminator(),
            bytes(range(32)),
            struct.pack("<Q", sol_amount),
            struct.pack("<Q", token_amount),
            b"\x01" if is_buy else b"\x00",
            b"\x11" * 32,
            struct.pack("<q", timestamp),
            struct.pack("<4Q", virtual_sol, virtual_token, real_sol, real_token),
            b"\x22" * 32,
            struct.pack("<Q", fee_bps),
            struct.pack("<Q", fee),
            b"\x33" * 32,
            struct.pack("<Q", 0),
            struct.pack("<Q", 0),
            trailing,
        ]
    )


# --------------------------------------------------------------------- decode
class TradeDecodeTests(unittest.TestCase):
    def test_packed_trade_event(self):
        raw = _pack_trade(trailing=b"\xff" * 12)
        parsed = decode_trade_event(raw)
        assert parsed is not None
        self.assertTrue(parsed["is_buy"])
        self.assertEqual(parsed["virtual_sol_reserves"], 30_050_000_000)
        self.assertEqual(parsed["real_token_reserves"], 792_100_000_000_000)
        self.assertEqual(parsed["fee_basis_points"], 100)
        self.assertEqual(trade_event_discriminator(), hashlib.sha256(b"event:TradeEvent").digest()[:8])
        self.assertIsNone(decode_trade_event(b"\x00" * 8 + raw[8:]))
        self.assertIsNone(decode_trade_event(raw[:100]))
        line = "Program data: " + base64.b64encode(raw).decode()
        self.assertTrue(extract_trade_from_logs(["Program log: Instruction: Buy", line])["is_buy"])

    def test_real_mainnet_trade_event(self):
        tx = _fixture("trade_event_tx.json")
        ev = extract_trade_from_logs(tx["logs"])
        assert ev is not None
        self.assertEqual(ev["mint"], REAL_MINT)
        self.assertFalse(ev["is_buy"])
        self.assertEqual(ev["sol_amount"], 75_099)
        self.assertEqual(ev["token_amount"], 2_685_969_901)
        self.assertEqual(ev["virtual_sol_reserves"], 30_000_537_370)
        self.assertEqual(ev["virtual_token_reserves"], 1_072_980_780_848_364)
        self.assertEqual(ev["real_token_reserves"], 793_080_780_848_364)
        self.assertEqual(ev["timestamp"], tx["blockTime"])
        self.assertEqual((ev["fee_basis_points"], ev["creator_fee_basis_points"]), (95, 30))


# ------------------------------------------------------------ verification
class VerifyTests(unittest.TestCase):
    def test_pda_vectors(self):
        self.assertEqual(find_program_address([b"global"], PUMP_PROGRAM_ID)[0], GLOBAL_PDA)
        self.assertEqual(
            find_program_address([b"mint-authority"], PUMP_PROGRAM_ID)[0],
            "TSLvdd1pWpHVjahSpsvCXUbgwsL3JAcvokwaKt1eokM",
        )
        acct = _fixture("bonding_curve_account.json")
        self.assertEqual(bonding_curve_address(acct["mint"]), acct["bonding_curve"])

    def _rpc(self, value):
        calls = []

        def rpc(method, params):
            calls.append((method, params))
            return {"value": value}

        return rpc, calls

    def test_real_account_accepted(self):
        acct = _fixture("bonding_curve_account.json")
        rpc, calls = self._rpc({"owner": PUMP_PROGRAM_ID, "data": [acct["data_b64"], "base64"]})
        res = verify_pump_mint(acct["mint"], rpc)
        self.assertTrue(res.ok, res)
        self.assertEqual(calls[0][0], "getAccountInfo")
        self.assertEqual(calls[0][1][0], acct["bonding_curve"])
        self.assertEqual(res["reserves"]["real_token"], 793_080_780_848_364)
        self.assertEqual(res["reserves"]["virtual_sol"], 30_000_537_370)

    def test_rejections(self):
        acct = _fixture("bonding_curve_account.json")
        blob = bytearray(base64.b64decode(acct["data_b64"]))
        rpc, _ = self._rpc(None)
        self.assertEqual(verify_pump_mint(acct["mint"], rpc)["reason"], "no_bonding_curve")
        rpc, _ = self._rpc({"owner": "11111111111111111111111111111111", "data": [acct["data_b64"], "base64"]})
        self.assertEqual(verify_pump_mint(acct["mint"], rpc)["reason"], "not_pump_program")
        blob[8 + 40] = 1  # complete flag
        rpc, _ = self._rpc({"owner": PUMP_PROGRAM_ID, "data": [base64.b64encode(bytes(blob)).decode(), "base64"]})
        self.assertEqual(verify_pump_mint(acct["mint"], rpc)["reason"], "graduated")
        self.assertEqual(verify_pump_mint("not-base58-0OIl", rpc)["reason"], "bad_mint")

        def down(method, params):
            raise OSError("unreachable")

        res = verify_pump_mint(acct["mint"], down)
        self.assertFalse(res.ok)
        self.assertTrue(res.retry)


# ----------------------------------------------------------- live provider
class LiveProviderTests(NoNetworkCase):
    def test_pending_until_verified_then_seeded_from_chain(self):
        seeded = {"virtual_sol": 31_000_000_000, "virtual_token": 1_038_000_000_000_000, "real_sol": 1_000_000_000, "real_token": 758_100_000_000_000}
        p = _live(verifier=_ok_verifier(seeded))
        p.register_watch_mint("PendMint11111111111111111111111111111111", base="PEND", source="pumpportal")
        self.assertEqual(p.list_symbols(), [])
        self.assertIsNone(p.get_pumpfun_snapshot("PEND/SOL"))
        self.assertEqual(p.verify_pending(), {"PendMint11111111111111111111111111111111": "ok"})
        snap = p.get_pumpfun_snapshot("PEND/SOL")
        assert snap is not None
        self.assertFalse(snap.synthetic)
        self.assertAlmostEqual(snap.price_sol, price_sol(31_000_000_000, 1_038_000_000_000_000))
        self.assertEqual(p.watch_flags("PEND/SOL")["market_source"], "real")

    def test_rejected_mint_dropped_and_refused(self):
        p = _live(verifier=lambda m: {"ok": False, "retry": False, "reason": "not_pump_program"})
        p.register_watch_mint("RayMint111111111111111111111111111111111", base="RAY", source="watch")
        self.assertEqual(p.verify_pending()["RayMint111111111111111111111111111111111"], "rejected:not_pump_program")
        self.assertEqual(p.list_symbols(), [])
        self.assertIsNone(p.register_watch_mint("RayMint111111111111111111111111111111111", source="watch"))
        self.assertIsNone(p.register_watch_mint("DemoMintPump11111111111111111111111111111"))
        self.assertIsNone(p.register_watch_mint("GradMint", complete=True))

    def test_rpc_down_keeps_pending(self):
        now = [1_000]
        p = _live(verifier=lambda m: {"ok": False, "retry": True, "reason": "rpc_unavailable"}, clock=lambda: now[0])
        p.register_watch_mint("WaitMint11111111111111111111111111111111", base="WAIT", source="pumpportal")
        self.assertEqual(p.verify_pending(), {"WaitMint11111111111111111111111111111111": "retry"})
        self.assertEqual(p.verify_pending(), {})  # backoff
        self.assertEqual(p.list_symbols(), [])
        self.assertEqual(p.pending_mints(), ["WaitMint11111111111111111111111111111111"])

    def test_create_event_source_is_tradable_immediately(self):
        p = _live(verifier=lambda m: self.fail("create-event mints need no RPC"))
        p.register_watch_mint("NewMint1111111111111111111111111111111111", base="NEW", source="logs")
        self.assertEqual([s.symbol for s in p.list_symbols()], ["NEW/SOL"])

    def test_price_moves_only_on_real_prints(self):
        now = [1_000]
        p = _live(clock=lambda: now[0])
        p.register_watch_mint("RealMint111111111111111111111111111111111", base="REAL", reserves=INIT, source="logs")
        px0 = p.get_pumpfun_snapshot("REAL/SOL").price_sol
        for _ in range(5):
            now[0] += 60_000
            self.assertEqual(p.get_pumpfun_snapshot("REAL/SOL").price_sol, px0)
        self.assertEqual(p.get_recent_trades("REAL/SOL"), [])
        self.assertEqual(p.get_candles("REAL/SOL", "1m"), [])
        self.assertIsNone(p.first_trade_after("REAL/SOL", 0))
        # A print without reserves is ignored rather than guessed.
        self.assertIsNone(p.apply_observed_trade({"mint": "RealMint111111111111111111111111111111111", "side": "buy"}))
        self.assertEqual(p.get_pumpfun_snapshot("REAL/SOL").price_sol, px0)
        tx = _fixture("trade_event_tx.json")
        p2 = _live(clock=lambda: now[0])
        p2.register_watch_mint(REAL_MINT, base="TICKER3", source="logs")
        row = p2.observe_logs(tx["logs"], signature=tx["signature"])
        assert row is not None
        self.assertEqual(row["side"], "sell")
        self.assertEqual(row["ts"], now[0])
        self.assertEqual(row["chain_ts"], tx["blockTime"] * 1000)
        snap = p2.get_pumpfun_snapshot("TICKER3/SOL")
        self.assertAlmostEqual(snap.price_sol, price_sol(30_000_537_370, 1_072_980_780_848_364))
        self.assertEqual(p2.fee_bps_for("TICKER3/SOL"), (95, 30))
        self.assertEqual(len(p2.get_candles("TICKER3/SOL", "1m")), 1)

    def test_portal_units_and_graduation(self):
        p = _live()
        mint = "PortMint111111111111111111111111111111111"
        p.register_watch_mint(mint, base="PORT", source="logs")
        msg = {
            "signature": "sig1",
            "mint": mint,
            "txType": "buy",
            "tokenAmount": 35215492.73,
            "solAmount": 1.0,
            "vTokensInBondingCurve": 1037784507.27,
            "vSolInBondingCurve": 31.018,
            "marketCapSol": 29.89,
            "pool": "pump",
        }
        row = p.observe_portal_message(msg, recv_ts=5_000)
        assert row is not None
        snap = p.get_pumpfun_snapshot("PORT/SOL")
        self.assertEqual(snap.virtual_sol_reserves, str(31_018_000_000))
        self.assertEqual(snap.virtual_token_reserves, str(1_037_784_507_270_000))
        # price * 1e6 == market cap in SOL for a 1B supply
        self.assertAlmostEqual(snap.price_sol * 1e6, 29.89, delta=0.01)
        self.assertEqual(int(snap.real_token_reserves), 1_037_784_507_270_000 - (INITIAL_VIRTUAL_TOKEN_RESERVES - INITIAL_REAL_TOKEN_RESERVES))
        self.assertEqual(row["qty"], tokens_to_raw(35215492.73) / LAMPORTS_PER_SOL)
        self.assertFalse(snap.complete)
        self.assertIsNone(p.observe_portal_message({**msg, "pool": "pump-amm"}))
        self.assertTrue(p.get_pumpfun_snapshot("PORT/SOL").complete)
        self.assertEqual(sol_to_lamports(31.018), 31_018_000_000)
        self.assertEqual(tokens_to_raw(1_073_000_000_000_000), 1_073_000_000_000_000)

    def test_provider_selected_by_env(self):
        from app.providers import get_provider, market_data_kind, reset_provider

        prev = os.environ.get("DATA_PROVIDER")
        try:
            os.environ["DATA_PROVIDER"] = "pumpfun_live_paper"
            reset_provider()
            self.assertIsInstance(get_provider(), PumpfunLivePaperProvider)
            self.assertEqual(market_data_kind(), "real")
            self.assertEqual(market_data_kind("pumpfun_paper"), "synthetic")
            self.assertEqual(market_data_kind("mock"), "mock")
        finally:
            if prev is None:
                os.environ.pop("DATA_PROVIDER", None)
            else:
                os.environ["DATA_PROVIDER"] = prev
            reset_provider()


class _FakeWS:
    def __init__(self, inbox):
        self.sent: list[dict] = []
        self.inbox = inbox

    async def send(self, raw):
        self.sent.append(json.loads(raw))

    async def recv(self):
        if self.inbox:
            return self.inbox.pop(0)
        await asyncio.sleep(0.01)
        raise asyncio.TimeoutError

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class PortalFeedTests(unittest.IsolatedAsyncioTestCase):
    async def test_subscribes_resubscribes_and_applies(self):
        p = _live()
        a = "MintA111111111111111111111111111111111111"
        b = "MintB111111111111111111111111111111111111"
        p.register_watch_mint(a, base="AAA", source="logs")
        inbox = [json.dumps({"message": "Successfully subscribed"}), json.dumps({
            "mint": a, "txType": "sell", "tokenAmount": 1000.0, "solAmount": 0.00003,
            "vTokensInBondingCurve": 1072000000.0, "vSolInBondingCurve": 30.03, "pool": "pump",
        })]
        ws = _FakeWS(inbox)

        class FakeWebsockets:
            uris: list[str] = []

            @staticmethod
            def connect(uri, **kw):
                FakeWebsockets.uris.append(uri)
                return ws

        p.portal_sync_sec = 0.0
        p._feed_running = True

        async def later():
            await asyncio.sleep(0.05)
            p.register_watch_mint(b, base="BBB", source="logs")
            await asyncio.sleep(0.1)
            p.stop_feed()

        await asyncio.gather(p._portal_session(FakeWebsockets, "wss://pumpportal.fun/api/data"), later())
        self.assertEqual(ws.sent[0], {"method": "subscribeTokenTrade", "keys": [a]})
        self.assertIn({"method": "subscribeTokenTrade", "keys": [b]}, ws.sent)
        self.assertEqual(len(p.get_recent_trades("AAA/SOL")), 1)
        self.assertAlmostEqual(p.get_pumpfun_snapshot("AAA/SOL").price_sol, price_sol(30_030_000_000, 1_072_000_000_000_000))

    async def test_auto_prefers_free_logs_and_portal_only_after_logs_failure(self):
        p = _live()
        calls: list[str] = []

        async def logs_once():
            calls.append("logs")
            if calls.count("logs") == 1:
                raise ConnectionError("rpc down")
            p.stop_feed()

        async def portal_once():
            calls.append("portal")
            p.stop_feed()

        p._feed_running = True
        with mock.patch.dict(os.environ, {"LIVE_PAPER_FEED": "auto"}), mock.patch.object(
            p, "_logs_once", side_effect=logs_once
        ), mock.patch.object(p, "_portal_once", side_effect=portal_once), mock.patch(
            "app.providers.pumpfun_live_paper.asyncio.sleep", new=mock.AsyncMock()
        ), mock.patch.object(PumpfunLivePaperProvider, "_discovery_forwards_logs", return_value=False):
            await p._trade_loop()
        self.assertEqual(calls, ["logs", "portal"])

    async def test_feed_off_does_nothing(self):
        p = _live()
        with mock.patch.dict(os.environ, {"LIVE_PAPER_FEED": "off"}), mock.patch(
            "websockets.connect", side_effect=AssertionError("network")
        ):
            await p.run_feed()
        self.assertEqual(p.feed_status, "off")


# ---------------------------------------------------------------- discovery
class DiscoveryTests(NoNetworkCase):
    def test_accepts_only_pump_non_graduated(self):
        sample = {"mint": "DiscMint111111111111111111111111111111111", "symbol": "NEW", "vSolInBondingCurve": 30.0, "vTokensInBondingCurve": 1_073_000_000.0}
        self.assertTrue(discovery_accepts(sample, "pumpportal"))
        self.assertFalse(discovery_accepts({**sample, "mint": "DemoMintNope"}, "pumpportal"))
        self.assertFalse(discovery_accepts({**sample, "complete": True}, "pumpportal"))
        self.assertFalse(discovery_accepts({**sample, "pool": "raydium"}, "pumpportal"))
        self.assertFalse(discovery_accepts({**sample, "pool": "bonk"}, "pumpportal"))
        self.assertTrue(discovery_accepts({**sample, "pool": "pump"}, "pumpportal"))
        ev = normalize_new_token(sample, "pumpportal")
        self.assertEqual(ev.initial_reserves["virtual_token_reserves"], str(1_073_000_000_000_000))

    def test_logs_trade_events_forwarded_to_live_provider(self):
        p = _live()
        p.register_watch_mint(REAL_MINT, base="TICKER3", source="logs")
        runtime = DiscoveryRuntime()
        tx = _fixture("trade_event_tx.json")
        with mock.patch("app.discovery.get_provider", return_value=p):
            runtime._forward_trade_logs(tx["logs"], tx["signature"])
        self.assertEqual(runtime.trade_logs_forwarded, 1)
        self.assertEqual(p.get_recent_trades("TICKER3/SOL")[0]["signature"], tx["signature"])
        # The synthetic provider has no observe_logs: nothing is forwarded.
        with mock.patch("app.discovery.get_provider", return_value=PumpfunPaperProvider(watch_mints="")):
            runtime._forward_trade_logs(tx["logs"], tx["signature"])
        self.assertEqual(runtime.trade_logs_forwarded, 1)


# ----------------------------------------------------- source labels / stats
class SourceLabelTests(NoNetworkCase):
    def setUp(self):
        super().setUp()
        reset_paper_ledger(wipe_store=True)
        reset_engine()
        reset_paper_broker()

    def tearDown(self):
        reset_paper_ledger(wipe_store=True)
        reset_engine()
        reset_paper_broker()
        super().tearDown()

    def test_broker_labels_every_fill(self):
        ctx = StrategyContext(symbol="X/SOL", ts=1, account=AccountCtx(), liquidity=LiquidityCtx(), tick=TickCtx(mid=1.0))
        intent = OrderIntent(side="buy", order_type="market", qty_or_notional=1.0, max_slippage_bps=500)
        with mock.patch("app.paper.broker._active_market_kind", return_value="mock"):
            self.assertEqual(PaperBroker().submit(ctx, intent)[0].market_source, "mock")
        with mock.patch("app.paper.broker._active_market_kind", return_value="synthetic"):
            self.assertEqual(PaperBroker().submit(ctx, intent)[0].market_source, "synthetic")
        labeled = ctx.model_copy(update={"meta": {"market_source": "real"}})
        self.assertEqual(PaperBroker().submit(labeled, intent)[0].market_source, "real")

    def test_non_real_sources_excluded_from_stats(self):
        journal = get_paper_journal()
        for i, src in enumerate(("synthetic", "mock", "real")):
            sym = f"S{i}/SOL"
            journal.record_fill(sym, Fill(ts=10 * i + 1, price=1.0, qty=1.0, fee=0.0, market_source=src, tag="paper:pump-paper-v1"))
            journal.record_fill(sym, Fill(ts=10 * i + 2, price=0.9, qty=-1.0, fee=0.0, market_source=src, tag="paper:pump-paper-v1:flat"))
        self.assertEqual([t.market_source for t in journal.closed], ["synthetic", "mock", "real"])
        perf = build_performance()
        self.assertEqual(perf["n_trades"], 1)
        self.assertEqual(perf["synthetic_n"], 1)
        self.assertEqual(perf["market_window"], "real")
        self.assertTrue(excluded_from_go({"market_source": "real", "phantom_short": True}))
        from app.paper.executability import build_executability

        self.assertEqual(build_executability(window="session")["n_closed"], 1)

    def test_legacy_rows_load_labeled_and_real_window_starts_at_zero(self):
        payload = {
            "equity_0": 10_000,
            "fills": [],
            "lots": {"OLD/SOL": [{"symbol": "OLD/SOL", "qty": -3.07, "price": 7e-5, "ts": 1, "fees": 0, "tag": "paper:pump-paper-v1:flat"}]},
            "closed": [
                {"id": "a", "strategy_id": "pump-paper-v1", "symbol": "OLD/SOL", "entry_ts": 1, "exit_ts": 2, "entry_price": 1.0, "exit_price": 1.06, "qty": 1.0, "pnl": 0.06, "pnl_pct": 0.06, "fees": 0.0, "tags": [], "source": "signal", "side": "long"},
            ],
        }
        tmp = tempfile.TemporaryDirectory(prefix="auu-legacy-")
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "paper_journal.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        journal = _load_journal(path)
        assert journal is not None
        self.assertEqual(journal.closed[0].market_source, "legacy_synthetic")
        self.assertEqual(journal.closed[0].as_dict()["market_source"], "legacy_synthetic")
        self.assertNotIn("market_source", journal.closed[0].as_dict(persist=True))
        self.assertEqual(journal.lots["OLD/SOL"][0].market_source, "legacy_synthetic")
        # A real buy on the same symbol does not settle the legacy residual short.
        closed = journal.record_fill("OLD/SOL", Fill(ts=5, price=8e-5, qty=400.0, fee=0.0, market_source="real", tag="paper:pump-paper-v1"))
        self.assertEqual(closed, [])
        self.assertEqual(sorted(l.qty for l in journal.lots["OLD/SOL"]), [-3.07, 400.0])
        loaded = RoundTrip.from_dict(payload["closed"][0])
        self.assertTrue(excluded_from_go(loaded))


# ------------------------------------------------------------- curve fills
def _curve_ctx(meta, *, held=0.0, vs=INITIAL_VIRTUAL_SOL_RESERVES, vt=INITIAL_VIRTUAL_TOKEN_RESERVES):
    return StrategyContext(
        symbol="REAL/SOL",
        ts=1_000,
        account=AccountCtx(),
        liquidity=LiquidityCtx(virtual_sol_reserves=str(vs), virtual_token_reserves=str(vt), real_token_reserves=str(INITIAL_REAL_TOKEN_RESERVES)),
        position=held,
        tick=TickCtx(mid=price_sol(vs, vt)),
        pump=PumpCtx(virtual_sol_reserves=str(vs), virtual_token_reserves=str(vt), real_sol_reserves="0", real_token_reserves=str(INITIAL_REAL_TOKEN_RESERVES)),
        meta=meta,
    )


class CurveFillTests(NoNetworkCase):
    def setUp(self):
        super().setUp()
        reset_paper_ledger(wipe_store=True)
        reset_paper_broker()
        reset_risk_gate()

    def tearDown(self):
        reset_paper_ledger(wipe_store=True)
        reset_paper_broker()
        super().tearDown()

    def test_buy_is_curve_quote_plus_fee(self):
        meta = {"curve_fill": True, "market_source": "real", "protocol_fee_bps": 95, "creator_fee_bps": 30}
        fills = PaperBroker().submit(_curve_ctx(meta), OrderIntent(side="buy", order_type="market", qty_or_notional=0.02, max_slippage_bps=10_000))
        self.assertEqual(len(fills), 1)
        lamports = 20_000_000
        tokens = buy_tokens_out(INITIAL_VIRTUAL_SOL_RESERVES, INITIAL_VIRTUAL_TOKEN_RESERVES, INITIAL_REAL_TOKEN_RESERVES, lamports, 95, 30)
        self.assertAlmostEqual(fills[0].qty, tokens / LAMPORTS_PER_SOL)
        after_fee = sol_after_buy_fee(lamports, 95, 30)
        # price excludes the fee; price*qty + fee is exactly the SOL spent.
        self.assertAlmostEqual(fills[0].price, after_fee / tokens, delta=after_fee / tokens * 1e-12)
        self.assertAlmostEqual(fills[0].fee, (lamports - after_fee) / LAMPORTS_PER_SOL)
        self.assertAlmostEqual(fills[0].price * fills[0].qty + fills[0].fee, 0.02, places=12)
        self.assertGreater(fills[0].price, price_sol(INITIAL_VIRTUAL_SOL_RESERVES, INITIAL_VIRTUAL_TOKEN_RESERVES))
        self.assertEqual(fills[0].market_source, "real")

    def test_sell_closes_held_quantity(self):
        held = 423.4719
        meta = {"curve_fill": True, "flatten_qty": held, "market_source": "real", "fill_ts": 5_000}
        ctx = _curve_ctx(meta, held=held)
        fills = PaperBroker().submit(ctx, OrderIntent(side="sell", order_type="market", qty_or_notional=held * ctx.tick.mid * 1.2, max_slippage_bps=10_000, client_tag="paper:pump-paper-v1:flat"))
        self.assertEqual(len(fills), 1)
        self.assertAlmostEqual(fills[0].qty, -held)
        # No fee observed on a TradeEvent: conservative 95 + 30 bps.
        net, gross = sell_sol_out(INITIAL_VIRTUAL_SOL_RESERVES, INITIAL_VIRTUAL_TOKEN_RESERVES, int(round(held * LAMPORTS_PER_SOL)), 95, 30)
        self.assertAlmostEqual(fills[0].price * held, gross / LAMPORTS_PER_SOL, places=9)
        self.assertAlmostEqual(fills[0].fee, (gross - net) / LAMPORTS_PER_SOL)
        self.assertAlmostEqual(fills[0].price * held - fills[0].fee, net / LAMPORTS_PER_SOL, places=9)
        self.assertEqual(fills[0].ts, 5_000)
        journal = get_paper_journal()
        journal.record_fill("REAL/SOL", Fill(ts=1, price=fills[0].price, qty=held, fee=0.0, market_source="real", tag="paper:pump-paper-v1"))
        journal.record_fill("REAL/SOL", fills[0])
        self.assertFalse(journal.lots.get("REAL/SOL"))
        self.assertEqual([t.side for t in journal.closed], ["long"])

    def test_formula_path_sell_also_uses_held_qty(self):
        held = 100.0
        ctx = StrategyContext(symbol="M/SOL", ts=1, account=AccountCtx(), liquidity=LiquidityCtx(), tick=TickCtx(mid=1.0), meta={"flatten_qty": held})
        fills = PaperBroker().submit(ctx, OrderIntent(side="sell", order_type="market", qty_or_notional=100.0, max_slippage_bps=500))
        self.assertAlmostEqual(fills[0].qty, -held)

    def test_fee_resolution(self):
        # Observed on a real TradeEvent: used as-is (including a real 0 creator fee).
        self.assertEqual(curve_fee_bps({"protocol_fee_bps": 93, "creator_fee_bps": 0}), (93, 0))
        self.assertEqual(curve_fee_bps({"protocol_fee_bps": 95, "creator_fee_bps": 30}), (95, 30))
        # PumpPortal-only path (no fee fields): conservative 95 + 30 = 125 bps.
        self.assertEqual(curve_fee_bps({}), (95, 30))
        self.assertEqual(sum(curve_fee_bps({})), 125)
        # Partially observed: the unknown half still gets its default.
        self.assertEqual(curve_fee_bps({"protocol_fee_bps": 93}), (93, 30))
        # A smaller snapshot creator fee or env value never lowers the default...
        self.assertEqual(curve_fee_bps({}, PumpCtx(creator_fee_bps=5)), (95, 30))
        with mock.patch.dict(os.environ, {"PAPER_CURVE_PROTOCOL_FEE_BPS": "50", "PAPER_CURVE_CREATOR_FEE_BPS": "0"}):
            self.assertEqual(curve_fee_bps({}), (95, 30))
        # ...a larger one raises it.
        self.assertEqual(curve_fee_bps({}, PumpCtx(creator_fee_bps=50)), (95, 50))
        with mock.patch.dict(os.environ, {"PAPER_CURVE_PROTOCOL_FEE_BPS": "120", "PAPER_CURVE_CREATOR_FEE_BPS": "40"}):
            self.assertEqual(curve_fee_bps({}), (120, 40))
        self.assertIsNone(quote_curve_fill(side="sell", notional_sol=1.0, virtual_sol_reserves=1, virtual_token_reserves=1, real_token_reserves=1))

    def test_round_trip_pnl_counts_fees_once(self):
        """Ledger pnl == SOL received - SOL spent (fee not double-counted)."""
        journal = get_paper_journal()
        buy_meta = {"curve_fill": True, "market_source": "real"}
        buy = PaperBroker().submit(_curve_ctx(buy_meta), OrderIntent(side="buy", order_type="market", qty_or_notional=0.05, max_slippage_bps=10_000, client_tag="paper:pump-paper-v1"))[0]
        journal.record_fill("REAL/SOL", buy)
        lamports = 50_000_000
        tokens = buy_tokens_out(INITIAL_VIRTUAL_SOL_RESERVES, INITIAL_VIRTUAL_TOKEN_RESERVES, INITIAL_REAL_TOKEN_RESERVES, lamports, 95, 30)
        after = sol_after_buy_fee(lamports, 95, 30)
        vs2, vt2 = INITIAL_VIRTUAL_SOL_RESERVES + after, INITIAL_VIRTUAL_TOKEN_RESERVES - tokens
        sell_meta = {"curve_fill": True, "market_source": "real", "flatten_qty": buy.qty}
        sell = PaperBroker().submit(_curve_ctx(sell_meta, held=buy.qty, vs=vs2, vt=vt2), OrderIntent(side="sell", order_type="market", qty_or_notional=1.0, max_slippage_bps=10_000, client_tag="paper:pump-paper-v1:flat"))[0]
        journal.record_fill("REAL/SOL", sell)
        net, _gross = sell_sol_out(vs2, vt2, tokens, 95, 30)
        trip = journal.closed[-1]
        self.assertAlmostEqual(trip.pnl, (net - lamports) / LAMPORTS_PER_SOL, places=12)
        self.assertLess(trip.pnl, 0)  # buy-then-sell into your own impact loses ~2x fees
        self.assertAlmostEqual(trip.pnl / 0.05 * 1e4, -250, delta=5)

    def test_latency_env(self):
        reset_paper_broker()
        with mock.patch.dict(os.environ, {"PAPER_FILL_LATENCY_MS": "750"}):
            self.assertEqual(get_paper_broker().latency_ms, 750)
        reset_paper_broker()


class LedgerResidualTests(NoNetworkCase):
    def setUp(self):
        super().setUp()
        reset_paper_ledger(wipe_store=True)

    def tearDown(self):
        reset_paper_ledger(wipe_store=True)
        super().tearDown()

    def test_oversell_does_not_open_short(self):
        journal = get_paper_journal()
        journal.record_fill("A/SOL", Fill(ts=1, price=1.0, qty=100.0, fee=0.0, market_source="real", tag="paper:pump-paper-v1"))
        journal.record_fill("A/SOL", Fill(ts=2, price=1.1, qty=-100.7, fee=0.0, market_source="real", tag="paper:pump-paper-v1:flat"))
        self.assertIsNone(journal.lots.get("A/SOL"))
        self.assertEqual([(t.side, t.qty) for t in journal.closed], [("long", 100.0)])
        self.assertAlmostEqual(journal.fills[-1]["clamped_residual_qty"], -0.7)
        # Next entry on the same symbol is a clean long, not a "short cover" win.
        journal.record_fill("A/SOL", Fill(ts=3, price=1.0, qty=50.0, fee=0.0, market_source="real", tag="paper:pump-paper-v1"))
        self.assertEqual(len(journal.closed), 1)
        self.assertEqual(journal.lots["A/SOL"][0].qty, 50.0)

    def test_real_sell_closes_held_legacy_long_without_short(self):
        journal = get_paper_journal()
        journal.record_fill("L/SOL", Fill(ts=1, price=1.0, qty=10.0, fee=0.0, market_source="legacy_synthetic", tag="manual"))
        journal.record_fill("L/SOL", Fill(ts=2, price=1.2, qty=-10.0, fee=0.0, market_source="real", tag="manual"))
        self.assertIsNone(journal.lots.get("L/SOL"))
        self.assertEqual(len(journal.closed), 1)
        self.assertEqual(journal.closed[0].market_source, "legacy_synthetic")
        self.assertTrue(excluded_from_go(journal.closed[0]))

    def test_real_buy_never_settles_a_legacy_short(self):
        journal = get_paper_journal()
        journal.record_fill("S/SOL", Fill(ts=1, price=1.0, qty=-3.0, fee=0.0, market_source="legacy_synthetic", tag="manual"))
        journal.record_fill("S/SOL", Fill(ts=2, price=1.0, qty=10.0, fee=0.0, market_source="real", tag="paper:pump-paper-v1"))
        self.assertEqual(journal.closed, [])
        real = [l for l in journal.lots["S/SOL"] if l.market_source == "real"]
        self.assertEqual(real[0].qty, 10.0)
        perf = build_performance()
        self.assertEqual(perf["open_lots"], 1)
        self.assertEqual(perf["open_lots_excluded"], 1)

    def test_strategy_sell_without_holding_opens_nothing(self):
        journal = get_paper_journal()
        journal.record_fill("B/SOL", Fill(ts=1, price=1.0, qty=-5.0, fee=0.0, market_source="real", tag="paper:pump-paper-v1:flat"))
        self.assertIsNone(journal.lots.get("B/SOL"))
        self.assertEqual(journal.closed, [])


# ------------------------------------------------------------ cleanup script
class LegacyCleanupTests(NoNetworkCase):
    def _files(self):
        tmp = tempfile.TemporaryDirectory(prefix="auu-cleanup-")
        self.addCleanup(tmp.cleanup)
        d = Path(tmp.name)
        journal = d / "paper_journal.json"
        shadow = d / "shadow_compare.json"
        journal.write_text(json.dumps({
            "equity_0": 10_000,
            "fills": [],
            "lots": {
                "A/SOL": [{"symbol": "A/SOL", "qty": -3.07, "price": 1.0, "ts": 1, "fees": 0, "tag": "paper:pump-paper-v1:flat"}],
                "M/SOL": [{"symbol": "M/SOL", "qty": -1.0, "price": 1.0, "ts": 1, "fees": 0, "tag": "paper:manual:source=manual"}],
            },
            "closed": [
                {"side": "short", "pnl": 0.001, "symbol": "A/SOL", "strategy_id": "pump-paper-v1", "tags": ["pump_paper_v1_entry"]},
                {"side": "long", "pnl": 0.06, "symbol": "A/SOL", "strategy_id": "pump-paper-v1", "market_source": "real"},
            ],
        }), encoding="utf-8")
        shadow.write_text(json.dumps({"v": 1, "closed": [{"set_id": "sniper", "pnl": 0.1}]}), encoding="utf-8")
        return journal, shadow

    def test_dry_run_writes_nothing(self):
        journal, shadow = self._files()
        before = (journal.read_bytes(), shadow.read_bytes())
        out = io.StringIO()
        report = cleanup_run(journal, shadow, apply=False, out=out)
        self.assertEqual((journal.read_bytes(), shadow.read_bytes()), before)
        self.assertEqual(report["phantom_short_lots_dropped"], 1)
        self.assertEqual(report["other_short_lots_kept"], 1)
        self.assertEqual(report["closed_labeled_legacy"], 1)
        self.assertEqual(report["closed_phantom_flagged"], 1)
        self.assertEqual(report["shadow_closed_labeled_legacy"], 1)
        self.assertIn("mode=dry-run", out.getvalue())
        self.assertEqual(sorted(p.name for p in journal.parent.iterdir()), ["paper_journal.json", "shadow_compare.json"])
        with mock.patch("sys.stdout", new=io.StringIO()):
            self.assertEqual(cleanup_main(["--journal", str(journal), "--shadow", str(shadow)]), 0)
        self.assertEqual((journal.read_bytes(), shadow.read_bytes()), before)

    def test_apply_backs_up_then_rewrites(self):
        journal, shadow = self._files()
        original = journal.read_bytes()
        report = cleanup_run(journal, shadow, apply=True, out=io.StringIO())
        self.assertEqual(len(report["backups"]), 2)
        backup = Path(next(b for b in report["backups"] if "paper_journal" in b))
        self.assertEqual(backup.read_bytes(), original)
        data = json.loads(journal.read_text(encoding="utf-8"))
        self.assertNotIn("A/SOL", data["lots"])
        self.assertEqual(data["lots"]["M/SOL"][0]["market_source"], "legacy_synthetic")
        self.assertTrue(data["closed"][0]["phantom_short"])
        self.assertEqual(data["closed"][0]["market_source"], "legacy_synthetic")
        self.assertEqual(data["closed"][1]["market_source"], "real")
        self.assertEqual(json.loads(shadow.read_text())["closed"][0]["market_source"], "legacy_synthetic")
        again, counts = clean_journal(data)
        self.assertEqual(again, data)
        self.assertEqual(counts["phantom_short_lots_dropped"], 0)


# ---------------------------------------------------------------- shadow
class ShadowProtectTests(NoNetworkCase):
    def setUp(self):
        super().setUp()
        reset_shadow_compare()
        reset_paper_ledger(wipe_store=True)
        reset_engine()

    def tearDown(self):
        reset_shadow_compare()
        reset_paper_ledger(wipe_store=True)
        reset_engine()
        super().tearDown()

    def _open_shadow(self, symbol, mint):
        _opens[("loose", symbol)] = _Open(
            symbol=symbol, mint=mint, entry_price=1e-8, entry_ts=1, entry_notional=0.1, tokens=1_000, sol_lamports=100_000_000
        )

    def test_synthetic_provider_keeps_shadow_curve(self):
        provider = PumpfunPaperProvider(watch_mints="")
        mint = "ShadowMint1111111111111111111111111111111"
        provider.register_watch_mint(mint, base="SHAD", progress_bps=2000, source="pumpportal", max_discovered=1)
        self._open_shadow("SHAD/SOL", mint)
        provider.register_watch_mint("OtherMint11111111111111111111111111111111", base="OTHR", progress_bps=2000, source="pumpportal", max_discovered=1)
        self.assertIn(mint, provider._by_mint)
        self.assertGreater(provider.get_pumpfun_snapshot("SHAD/SOL").progress_bps, 1000)

    def test_live_provider_keeps_shadow_curve_and_its_reserves(self):
        p = _live()
        mint = "ShadowLive111111111111111111111111111111"
        p.register_watch_mint(mint, base="SHAD", source="logs", max_discovered=1)
        p.apply_observed_trade({"mint": mint, "side": "buy", "ts": 5, "virtual_sol_reserves": 40_000_000_000, "virtual_token_reserves": 804_750_000_000_000})
        px = p.get_pumpfun_snapshot("SHAD/SOL").price_sol
        self._open_shadow("SHAD/SOL", mint)
        p.register_watch_mint("OtherLive11111111111111111111111111111111", base="OTHR", source="logs", max_discovered=1)
        snap = p.get_pumpfun_snapshot("SHAD/SOL")
        self.assertIsNotNone(snap)
        self.assertEqual(snap.price_sol, px)  # not re-initialised to the template curve
        self.assertIn("OTHR/SOL", {s.symbol for s in p.list_symbols()})
        _opens.clear()
        p.register_watch_mint("ThirdLive11111111111111111111111111111111", base="THRD", source="logs", max_discovered=1)
        self.assertNotIn("OTHR/SOL", {s.symbol for s in p.list_symbols()})

    def test_synthetic_snapshot_skips_shadow_column(self):
        apply_shadow_config({"enabled": True, "sets": [{"id": "loose", "min_trade_count_1m": 1, "min_buy_sell_ratio_1m": 0.5}]})
        snap = PumpfunPaperSnapshot(
            mint="DemoMintPump11111111111111111111111111111",
            symbol="PUMPDEMO/SOL",
            phase="curve",
            progress_bps=4200,
            virtual_sol_reserves=str(INITIAL_VIRTUAL_SOL_RESERVES),
            virtual_token_reserves=str(INITIAL_VIRTUAL_TOKEN_RESERVES),
            real_sol_reserves="5000000000",
            real_token_reserves=str(INITIAL_REAL_TOKEN_RESERVES),
            token_total_supply="1000000000000000",
            price_sol=price_sol(INITIAL_VIRTUAL_SOL_RESERVES, INITIAL_VIRTUAL_TOKEN_RESERVES),
            updated_ts=1,
            synthetic=True,
        )
        tape = TapeWindow(buy_notional_1m=4.0, sell_notional_1m=1.0, trade_count_1m=12)
        observe_candidate(symbol=snap.symbol, snapshot=snap, tape=tape, now_ms=1_700_000_000_000, notional_sol=0.12, impact_entry_bps=20.0, main_params=PumpPaperParams())
        rich = snap.model_copy(update={"virtual_sol_reserves": str(int(INITIAL_VIRTUAL_SOL_RESERVES * 1.25)), "price_sol": price_sol(int(INITIAL_VIRTUAL_SOL_RESERVES * 1.25), INITIAL_VIRTUAL_TOKEN_RESERVES)})
        observe_candidate(symbol=snap.symbol, snapshot=rich, tape=tape, now_ms=1_700_000_005_000, notional_sol=0.12, impact_entry_bps=20.0, main_params=PumpPaperParams())
        self.assertEqual(len(shadow_closed("loose")), 1)
        self.assertEqual(shadow_closed("loose")[0]["market_source"], "synthetic")
        col = next(s for s in build_shadow_compare()["sets"] if s["id"] == "loose")
        self.assertEqual(col["n"], 0)


# ------------------------------------------------------------ deferred fills
class DeferredFillTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._patches = _no_network()
        for p in self._patches:
            p.start()
        reset_paper_ledger(wipe_store=True)
        reset_paper_broker()
        reset_risk_gate()
        reset_engine()
        self._env = mock.patch.dict(os.environ, {}, clear=False)
        self._env.start()
        os.environ.pop("AUTO_PAPER_ORDERS", None)

    def tearDown(self):
        self._env.stop()
        reset_paper_ledger(wipe_store=True)
        reset_paper_broker()
        reset_engine()
        for p in self._patches:
            p.stop()

    def _setup(self):
        provider = _live()
        mint = "DelayMint11111111111111111111111111111111"
        provider.register_watch_mint(mint, base="DLAY", reserves=INIT, source="logs")
        engine = PumpPaperEngine(PumpPaperParams(auto_paper_orders=True, max_notional_sol=0.05))
        return provider, mint, engine

    async def test_fills_on_first_trade_after_latency(self):
        provider, mint, engine = self._setup()
        snap = provider.get_pumpfun_snapshot("DLAY/SOL")
        now = 1_700_000_000_000
        signal = SignalOut(side="long", strength=0.8, reason="pump_paper_v1_entry", tags=["ENTRY"])
        await engine.maybe_execute("DLAY/SOL", snap, signal, now, 0.005)
        self.assertEqual(engine.positions, {})
        self.assertIn("DLAY/SOL", engine._deferred)
        # No print yet: nothing fills, however long we wait (inside the TTL).
        await engine._drain_real_fill(provider, "DLAY/SOL", now + 5_000)
        self.assertEqual(engine.positions, {})
        early_vs, early_vt = INITIAL_VIRTUAL_SOL_RESERVES + 10_000_000, INITIAL_VIRTUAL_TOKEN_RESERVES - 1_000_000
        provider.apply_observed_trade({"mint": mint, "side": "buy", "ts": now + 100, "virtual_sol_reserves": early_vs, "virtual_token_reserves": early_vt, "sol_amount": 0.01, "token_amount": 1_000_000})
        await engine._drain_real_fill(provider, "DLAY/SOL", now + 200)
        self.assertEqual(engine.positions, {}, "a print before decision+latency must not fill")
        late_vs, late_vt = INITIAL_VIRTUAL_SOL_RESERVES + 80_000_000, INITIAL_VIRTUAL_TOKEN_RESERVES - 8_000_000
        provider.apply_observed_trade({"mint": mint, "side": "buy", "ts": now + 400, "virtual_sol_reserves": late_vs, "virtual_token_reserves": late_vt, "sol_amount": 0.08, "token_amount": 8_000_000})
        provider.apply_observed_trade({"mint": mint, "side": "buy", "ts": now + 450, "virtual_sol_reserves": late_vs * 2, "virtual_token_reserves": late_vt // 2, "sol_amount": 30, "token_amount": 8_000_000})
        await engine._drain_real_fill(provider, "DLAY/SOL", now + 500)
        self.assertIn("DLAY/SOL", engine.positions)
        self.assertNotIn("DLAY/SOL", engine._deferred)
        pos = engine.positions["DLAY/SOL"]
        lamports = int(0.005 * LAMPORTS_PER_SOL)
        tokens = buy_tokens_out(late_vs, late_vt, INITIAL_REAL_TOKEN_RESERVES, lamports, 95, 30)
        self.assertAlmostEqual(pos.qty, tokens / LAMPORTS_PER_SOL, places=6)
        after = sol_after_buy_fee(lamports, 95, 30)
        self.assertAlmostEqual(pos.entry_price, after / tokens, delta=after / tokens * 1e-6)
        lot = get_paper_journal().lots["DLAY/SOL"][0]
        self.assertEqual(lot.market_source, "real")
        self.assertEqual(lot.ts, now + 400)

    async def test_entry_without_print_expires(self):
        provider, mint, engine = self._setup()
        snap = provider.get_pumpfun_snapshot("DLAY/SOL")
        now = 1_700_000_000_000
        signal = SignalOut(side="long", strength=0.8, reason="pump_paper_v1_entry", tags=["ENTRY"])
        with mock.patch.dict(os.environ, {"LIVE_PAPER_ENTRY_TTL_MS": "2000"}):
            await engine.maybe_execute("DLAY/SOL", snap, signal, now, 0.005)
            await engine._drain_real_fill(provider, "DLAY/SOL", now + 1_000)
            self.assertIn("DLAY/SOL", engine._deferred)
            await engine._drain_real_fill(provider, "DLAY/SOL", now + 3_000)
        self.assertNotIn("DLAY/SOL", engine._deferred)
        self.assertEqual(engine.positions, {})
        self.assertIsNone(get_paper_journal().lots.get("DLAY/SOL"))


def _logs_line(blob: bytes) -> str:
    return "Program data: " + base64.b64encode(blob).decode()


PACK_MINT = decode_trade_event(_pack_trade())["mint"]


class FeedDedupeTests(NoNetworkCase):
    def test_same_trade_from_portal_and_logs_counts_once(self):
        p = _live()
        p.register_watch_mint(PACK_MINT, base="DUP", source="logs")
        base = {"mint": PACK_MINT, "side": "buy", "virtual_sol_reserves": 30_050_000_000, "virtual_token_reserves": 1_072_000_000_000_000, "signature": "SIG1"}
        self.assertIsNotNone(p.apply_observed_trade({**base, "ts": 1, "feed": "logs"}))
        self.assertIsNone(p.apply_observed_trade({**base, "ts": 2, "feed": "pumpportal"}))
        self.assertEqual(len(p.get_recent_trades("DUP/SOL")), 1)
        self.assertEqual(p.feed_health()["duplicatesDropped"], 1)
        self.assertEqual(p.feed_health()["tradesByFeed"], {"logs": 1})
        # Two same-side trades in one tx from one feed are two prints; the
        # other feed's copies of both are dropped.
        self.assertIsNotNone(p.apply_observed_trade({**base, "ts": 3, "feed": "logs", "signature": "SIG2"}))
        self.assertIsNotNone(p.apply_observed_trade({**base, "ts": 4, "feed": "logs", "signature": "SIG2"}))
        self.assertIsNone(p.apply_observed_trade({**base, "ts": 5, "feed": "pumpportal", "signature": "SIG2"}))
        self.assertIsNone(p.apply_observed_trade({**base, "ts": 6, "feed": "pumpportal", "signature": "SIG2"}))
        self.assertEqual(len(p.get_recent_trades("DUP/SOL")), 3)
        # Unsigned rows are never deduped.
        unsigned = {k: v for k, v in base.items() if k != "signature"}
        self.assertIsNotNone(p.apply_observed_trade({**unsigned, "ts": 7, "feed": "pumpportal"}))
        self.assertIsNotNone(p.apply_observed_trade({**unsigned, "ts": 8, "feed": "pumpportal"}))

    def test_every_trade_event_in_a_tx_is_applied(self):
        p = _live()
        p.register_watch_mint(PACK_MINT, base="MULT", source="logs")
        first = _pack_trade(virtual_sol=30_100_000_000, virtual_token=1_070_000_000_000_000, real_token=790_000_000_000_000)
        second = _pack_trade(is_buy=False, virtual_sol=30_060_000_000, virtual_token=1_071_000_000_000_000, real_token=791_000_000_000_000)
        logs = ["Program log: Instruction: Buy", _logs_line(first), "Program log: Instruction: Sell", _logs_line(second)]
        self.assertEqual(len(extract_trades_from_logs(logs)), 2)
        row = p.observe_logs(logs, signature="MULTISIG")
        self.assertEqual(row["side"], "sell")
        self.assertEqual([r["side"] for r in p.get_recent_trades("MULT/SOL")], ["sell", "buy"])
        self.assertAlmostEqual(p.get_pumpfun_snapshot("MULT/SOL").price_sol, price_sol(30_060_000_000, 1_071_000_000_000_000))


class DiscoveryLogsOrderTests(unittest.IsolatedAsyncioTestCase):
    async def test_create_registered_before_its_trade_and_failed_tx_ignored(self):
        p = _live()
        trade = _logs_line(_pack_trade(virtual_sol=30_500_000_000, virtual_token=1_060_000_000_000_000, real_token=780_000_000_000_000))

        def fake_create(logs):
            if any("FAKE_CREATE" in x for x in logs):
                return {"mint": PACK_MINT, "symbol": "NEWT", "name": "New"}
            return None

        def note(sig, logs, err=None):
            return json.dumps({"params": {"result": {"context": {"slot": 1}, "value": {"signature": sig, "logs": logs, "err": err}}}})

        ok_logs = ["Program log: FAKE_CREATE", trade]
        inbox = [note("FAILED", ok_logs, err={"InstructionError": [0, "x"]}), note("CREATESIG", ok_logs)]
        ws = _FakeWS(list(inbox))
        runtime = DiscoveryRuntime()
        runtime._running = True

        async def stopper():
            for _ in range(100):
                await asyncio.sleep(0.01)
                if not ws.inbox:
                    break
            await asyncio.sleep(0.02)
            runtime._running = False

        with mock.patch("app.discovery.get_provider", return_value=p), mock.patch(
            "app.discovery.extract_create_from_logs", side_effect=fake_create
        ), mock.patch("websockets.connect", return_value=ws), mock.patch.dict(os.environ, {"SOLANA_RPC_URL": "https://rpc.invalid"}):
            await asyncio.gather(runtime._run_logs(), stopper())
        trades = p.get_recent_trades("NEWT/SOL")
        self.assertEqual([t["signature"] for t in trades], ["CREATESIG"])
        self.assertAlmostEqual(p.get_pumpfun_snapshot("NEWT/SOL").price_sol, price_sol(30_500_000_000, 1_060_000_000_000_000))


class ShadowDeferredTests(NoNetworkCase):
    def setUp(self):
        super().setUp()
        reset_shadow_compare()
        reset_paper_ledger(wipe_store=True)
        reset_paper_broker()
        reset_engine()
        self.p = _live()
        self.mint = "ShadowDefer11111111111111111111111111111"
        self.p.register_watch_mint(self.mint, base="SDEF", reserves=INIT, source="logs")
        for target in ("app.providers.get_provider", "app.strategies.pump_paper_v1.get_provider"):
            patch = mock.patch(target, return_value=self.p)
            patch.start()
            self.addCleanup(patch.stop)
        kind = mock.patch("app.providers.market_data_kind", return_value="real")
        kind.start()
        self.addCleanup(kind.stop)

    def tearDown(self):
        reset_shadow_compare()
        reset_paper_ledger(wipe_store=True)
        reset_paper_broker()
        reset_engine()
        super().tearDown()

    def _snap(self, vs, vt, rs="5000000000"):
        return PumpfunPaperSnapshot(
            mint=self.mint,
            symbol="SDEF/SOL",
            phase="curve",
            progress_bps=4200,
            virtual_sol_reserves=str(vs),
            virtual_token_reserves=str(vt),
            real_sol_reserves=rs,
            real_token_reserves=str(INITIAL_REAL_TOKEN_RESERVES),
            token_total_supply="1000000000000000",
            price_sol=price_sol(vs, vt),
            updated_ts=1,
            synthetic=False,
        )

    def _observe(self, snap, now):
        tape = TapeWindow(buy_notional_1m=4.0, sell_notional_1m=1.0, trade_count_1m=12)
        observe_candidate(symbol=snap.symbol, snapshot=snap, tape=tape, now_ms=now, notional_sol=0.12, impact_entry_bps=20.0, main_params=PumpPaperParams())

    def _trade(self, ts, vs, vt, side="buy"):
        self.p.apply_observed_trade({"mint": self.mint, "side": side, "ts": ts, "virtual_sol_reserves": vs, "virtual_token_reserves": vt, "sol_amount": 0.1, "token_amount": 1_000})

    def test_shadow_fills_on_first_real_print_after_latency(self):
        apply_shadow_config({"enabled": True, "sets": [{"id": "loose", "min_trade_count_1m": 1, "min_buy_sell_ratio_1m": 0.5}]})
        vs0, vt0 = INITIAL_VIRTUAL_SOL_RESERVES, INITIAL_VIRTUAL_TOKEN_RESERVES
        now = 1_700_000_000_000
        self._observe(self._snap(vs0, vt0), now)
        self.assertEqual(dict(_opens), {}, "entry waits for a real print")
        self._trade(now + 100, vs0 + 1_000_000, vt0 - 30_000_000)  # before decision + 300 ms
        self._observe(self._snap(vs0, vt0), now + 200)
        self.assertEqual(dict(_opens), {})
        fill_vs, fill_vt = vs0 + 2_000_000_000, vt0 - 66_000_000_000_000
        self._trade(now + 350, fill_vs, fill_vt)
        self._observe(self._snap(fill_vs, fill_vt), now + 400)
        pos = _opens[("loose", "SDEF/SOL")]
        self.assertEqual(pos.entry_ts, now + 350)
        expected = buy_tokens_out(fill_vs, fill_vt, fill_vt - (INITIAL_VIRTUAL_TOKEN_RESERVES - INITIAL_REAL_TOKEN_RESERVES), 120_000_000, 95, 30)
        self.assertEqual(pos.tokens, expected)
        # Take-profit decision: the exit also waits for the next real print.
        rich_vs = int(fill_vs * 1.3)
        self._observe(self._snap(rich_vs, fill_vt), now + 5_000)
        self.assertIn(("loose", "SDEF/SOL"), _opens)
        self.assertEqual(shadow_closed("loose"), [])
        exit_vs, exit_vt = int(fill_vs * 1.2), fill_vt
        self._trade(now + 5_400, exit_vs, exit_vt, side="sell")
        self._observe(self._snap(exit_vs, exit_vt), now + 5_500)
        rows = shadow_closed("loose")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["exit_ts"], now + 5_400)
        self.assertEqual(rows[0]["market_source"], "real")
        net, _g = sell_sol_out(exit_vs, exit_vt, expected, 95, 30)
        self.assertAlmostEqual(rows[0]["pnl"], (net - 120_000_000) / LAMPORTS_PER_SOL, places=12)

    def test_pending_entry_expires_and_protects_curve(self):
        from app.paper.shadow_compare import shadow_open_for

        apply_shadow_config({"enabled": True, "sets": [{"id": "loose", "min_trade_count_1m": 1, "min_buy_sell_ratio_1m": 0.5}]})
        now = 1_700_000_000_000
        snap = self._snap(INITIAL_VIRTUAL_SOL_RESERVES, INITIAL_VIRTUAL_TOKEN_RESERVES)
        with mock.patch.dict(os.environ, {"LIVE_PAPER_ENTRY_TTL_MS": "2000"}):
            self._observe(snap, now)
            self.assertTrue(shadow_open_for("SDEF/SOL", self.mint))
            self._observe(snap, now + 1_000)
            self.assertTrue(shadow_open_for("SDEF/SOL"))
            self._observe(snap, now + 3_000)  # TTL passed with no print: dropped, then re-decided
        from app.paper.shadow_compare import shadow_decisions

        self.assertIn("no_real_print", [d["reason"] for d in shadow_decisions()])
        self.assertEqual(dict(_opens), {})


class SourceGuardTests(unittest.TestCase):
    def test_no_send_paths(self):
        import inspect

        import app.discovery as disc
        from app.providers import pump_verify, pumpfun_live_paper as mod

        for m in (mod, pump_verify):
            src = inspect.getsource(m)
            self.assertNotIn("sendTransaction", src)
            self.assertNotIn("private", src.lower().replace("never", ""))
        self.assertIn('"subscribeTokenTrade"', inspect.getsource(mod))
        self.assertNotIn('"subscribeTokenTrade"', inspect.getsource(disc))


if __name__ == "__main__":
    unittest.main()
