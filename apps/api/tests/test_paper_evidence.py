"""Evidence logging for real paper trades and the offline replay.

No network, no real store: every file lives in a per-test temp dir.
"""
from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from app.models.contracts import SignalOut
from app.paper import evidence as ev
from app.legacy.pump.paper import replay
from app.paper.broker import reset_paper_broker
from app.paper.ledger import get_paper_journal, reset_paper_ledger
from app.legacy.pump.paper.real_fill import quote_curve_fill
from app.legacy.pump.providers.pumpfun_curve_math import (
    INITIAL_REAL_TOKEN_RESERVES,
    INITIAL_VIRTUAL_SOL_RESERVES,
    INITIAL_VIRTUAL_TOKEN_RESERVES,
    price_sol,
)
from app.legacy.pump.providers.pumpfun_live_paper import PumpfunLivePaperProvider
from app.risk.gate import reset_risk_gate
from app.legacy.pump.strategies import pump_paper_v1
from app.legacy.pump.strategies.pump_paper_v1 import PumpPaperEngine, PumpPaperParams, reset_engine

VS0 = INITIAL_VIRTUAL_SOL_RESERVES
VT0 = INITIAL_VIRTUAL_TOKEN_RESERVES
K = VS0 * VT0
MINT = "EvidMint1111111111111111111111111111111111"
CREATOR = "Creator1111111111111111111111111111111111"
INIT = {"virtual_sol": VS0, "virtual_token": VT0, "real_sol": 0, "real_token": INITIAL_REAL_TOKEN_RESERVES}
T0 = 1_700_000_000_000


def _vs_for_price(mult: float) -> tuple[int, int]:
    """Reserves on the standard curve whose spot is ``mult`` × the 30-SOL start."""
    vs = int(VS0 * mult ** 0.5)
    return vs, K // vs


def _print(provider, ts, mult, side="buy", sol=0.1, trader="buyerA"):
    vs, vt = _vs_for_price(mult)
    return provider.apply_observed_trade(
        {"mint": MINT, "side": side, "ts": ts, "virtual_sol_reserves": vs, "virtual_token_reserves": vt,
         "sol_amount": sol, "token_amount": 1_000_000, "trader": trader, "signature": f"sig{ts}{side}"}
    )


def _no_network():
    def boom(*_a, **_k):
        raise AssertionError("network access in a unit test")

    return [mock.patch("httpx.post", side_effect=boom), mock.patch("httpx.get", side_effect=boom),
            mock.patch("websockets.connect", side_effect=boom)]


class _Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._patches = _no_network()
        for p in self._patches:
            p.start()
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Path(self.tmp.name) / "evidence" / "paper_evidence.jsonl"
        self._env = mock.patch.dict(os.environ, {"PAPER_EVIDENCE_STORE": str(self.store)}, clear=False)
        self._env.start()
        for key in ("AUTO_PAPER_ORDERS", "AUU_EVIDENCE", "AUU_EVIDENCE_MAX_MB", "AUU_EVIDENCE_KEEP"):
            os.environ.pop(key, None)
        # These tests pin the 30 s post-exit window of evidence v1/v2.
        os.environ["AUU_EVIDENCE_POST_MS"] = "30000"
        ev.reset_evidence_recorder()
        reset_paper_ledger(wipe_store=True)
        reset_paper_broker()
        reset_risk_gate()
        reset_engine()

    def tearDown(self):
        ev.reset_evidence_recorder()
        reset_paper_ledger(wipe_store=True)
        reset_paper_broker()
        reset_engine()
        self._env.stop()
        for p in self._patches:
            p.stop()
        self.tmp.cleanup()

    def _records(self):
        return list(replay.iter_records(ev.evidence_files(self.store)))


class EngineEvidenceTests(_Base):
    async def _run_trade(self, *, evidence_on=True):
        """Entry at +1 s, TP exit signal at +4 s; prints from −40 s to +40 s after exit."""
        os.environ["AUU_EVIDENCE"] = "on" if evidence_on else "off"
        ev.reset_evidence_recorder()
        clock = {"now": T0}
        provider = PumpfunLivePaperProvider(watch_mints="", clock=lambda: clock["now"])
        provider.register_watch_mint(MINT, base="EVID", reserves=INIT, source="logs", creator=CREATOR)
        engine = PumpPaperEngine(PumpPaperParams(auto_paper_orders=True, max_notional_sol=0.05))
        sym = "EVID/SOL"
        with mock.patch.object(pump_paper_v1, "get_provider", return_value=provider):
            # Pre-signal tape: out of the 30 s window, creator buy, a sell, a momentum ramp.
            _print(provider, T0 - 40_000, 1.60, trader="old")
            _print(provider, T0 - 20_000, 1.70, sol=0.5, trader=CREATOR)
            _print(provider, T0 - 10_000, 1.75, side="sell", sol=0.2, trader="sellerX")
            _print(provider, T0 - 4_000, 1.78, sol=0.3, trader="buyerA")
            _print(provider, T0 - 2_000, 1.80, sol=0.4, trader="buyerB")
            _print(provider, T0 - 1_000, 1.81, sol=0.4, trader="buyerA")
            snap = provider.get_pumpfun_snapshot(sym)
            entry = SignalOut(side="long", strength=0.8, reason="pump_paper_v1_entry", tags=["pump-paper-v1"])
            await engine.maybe_execute(sym, snap, entry, T0, 0.005, entry_impact_bps=70.0)
            _print(provider, T0 + 100, 1.82)          # before signal + 300 ms latency: must not fill
            _print(provider, T0 + 1_000, 1.83)        # first print after latency → entry fill
            await engine._drain_real_fill(provider, sym, T0 + 1_100)
            self.assertIn(sym, engine.positions)
            _print(provider, T0 + 2_000, 1.90)
            _print(provider, T0 + 3_500, 2.00)
            snap = provider.get_pumpfun_snapshot(sym)
            flat = SignalOut(side="flat", strength=0.9, reason="take_profit", tags=["TAKE_PROFIT"])
            await engine.maybe_execute(sym, snap, flat, T0 + 4_000, 0.005)
            _print(provider, T0 + 4_500, 1.95, side="sell")   # first print after exit latency → exit fill
            await engine._drain_real_fill(provider, sym, T0 + 4_600)
            self.assertNotIn(sym, engine.positions)
            _print(provider, T0 + 20_000, 1.97)
            _print(provider, T0 + 34_000, 1.99)       # within exit + 30 s
            _print(provider, T0 + 40_000, 2.10)       # after exit + 30 s: excluded
            engine._poll_evidence(provider, T0 + 30_000)
            self.assertEqual(self._records(), [], "must wait for exit + 30 s")
            engine._poll_evidence(provider, T0 + 35_000)
        return engine, provider

    async def test_full_trade_is_recorded(self):
        await self._run_trade()
        recs = self._records()
        self.assertEqual(len(recs), 1)
        r = recs[0]
        self.assertEqual((r["symbol"], r["mint"], r["market_source"]), ("EVID/SOL", MINT, "real"))
        e, x = r["entry"], r["exit"]
        self.assertEqual(e["signal_ts"], T0)
        self.assertEqual(e["ready_ts"], T0 + 300)
        self.assertEqual(e["first_print_ts"], T0 + 1_000)
        self.assertEqual(e["fill_ts"], T0 + 1_000)
        self.assertEqual(e["signal_to_fill_ms"], 1_000)
        self.assertEqual(e["signal_reason"], "pump_paper_v1_entry")
        self.assertEqual(x["signal_ts"], T0 + 4_000)
        self.assertEqual(x["first_print_ts"], T0 + 4_500)
        self.assertEqual(x["fill_ts"], T0 + 4_500)
        self.assertEqual(x["reason"], "take_profit")
        self.assertAlmostEqual(x["trigger_price"], price_sol(*_vs_for_price(2.00)))
        self.assertLess(x["fill_vs_trigger_bps"], 0, "fill below the trigger mark after the dip")
        self.assertEqual(r["params"]["take_profit_pct"], 0.06)
        # Features at signal time.
        f = r["features"]
        self.assertEqual(f["token"]["discovery_source"], "logs")
        self.assertEqual(f["token"]["creator"], CREATOR)
        self.assertEqual(f["token"]["age_basis"], "create_event_received")
        self.assertIsNotNone(f["token"]["age_s"])
        self.assertEqual(f["tape"]["5s"]["trades"], 3)
        self.assertEqual(f["tape"]["5s"]["unique_buyers"], 2)
        self.assertEqual(f["tape"]["15s"]["sells"], 1)
        self.assertEqual(f["tape"]["30s"]["trades"], 5)
        self.assertEqual(f["tape"]["60s"]["trades"], 6)
        self.assertAlmostEqual(f["tape"]["30s"]["buy_sol"], 1.6)
        self.assertTrue(f["creator_buy"]["observed"])
        self.assertAlmostEqual(f["creator_buy"]["sol"], 0.5)
        self.assertGreater(f["momentum"]["30s_bps"], 0)
        self.assertIsNotNone(f["curve"]["market_cap_sol"])
        self.assertIsNotNone(f["curve"]["progress_bps"])
        # Tape: [signal − 30 s, exit + 30 s] only.
        tape = r["tape"]
        abs_ts = [tape["t0"] + row[0] for row in tape["rows"]]
        self.assertEqual(min(abs_ts), T0 - 20_000)
        self.assertEqual(max(abs_ts), T0 + 34_000)
        self.assertEqual(len(abs_ts), 12)
        self.assertEqual(tape["fields"], ["dt_ms", "side", "sol", "vs", "vt", "who"])
        self.assertIn(CREATOR[:8], [row[5] for row in tape["rows"]])
        # Path and result agree with the paper journal.
        self.assertGreater(r["path"]["mfe_bps"], 0)
        closed = [t for t in get_paper_journal().closed if t.market_source == "real"]
        self.assertEqual(len(closed), 1)
        self.assertAlmostEqual(r["result"]["net_sol"], closed[0].pnl, places=9)
        self.assertEqual(e["fill_ts"], closed[0].entry_ts)

    async def test_decisions_identical_with_evidence_off(self):
        await self._run_trade(evidence_on=True)
        on = [(t.entry_ts, t.exit_ts, t.qty, t.pnl, t.fees) for t in get_paper_journal().closed]
        reset_paper_ledger(wipe_store=True)
        reset_paper_broker()
        reset_risk_gate()
        self.store.unlink()
        await self._run_trade(evidence_on=False)
        off = [(t.entry_ts, t.exit_ts, t.qty, t.pnl, t.fees) for t in get_paper_journal().closed]
        self.assertEqual(on, off)
        self.assertEqual(self._records(), [])
        self.assertFalse(self.store.exists())

    async def test_recorder_errors_never_block_trading(self):
        class Broken(ev.EvidenceRecorder):
            def on_signal(self, **_k):
                raise RuntimeError("boom")

            def on_fills(self, **_k):
                raise RuntimeError("boom")

        broken = Broken()
        with mock.patch.object(ev, "get_evidence_recorder", return_value=broken), self.assertLogs("auu.paper.evidence", "ERROR"):
            provider = PumpfunLivePaperProvider(watch_mints="")
            provider.register_watch_mint(MINT, base="EVID", reserves=INIT, source="logs")
            engine = PumpPaperEngine(PumpPaperParams(auto_paper_orders=True, max_notional_sol=0.05))
            with mock.patch.object(pump_paper_v1, "get_provider", return_value=provider):
                snap = provider.get_pumpfun_snapshot("EVID/SOL")
                sig = SignalOut(side="long", strength=0.8, reason="pump_paper_v1_entry", tags=[])
                await engine.maybe_execute("EVID/SOL", snap, sig, T0, 0.005)
                _print(provider, T0 + 500, 1.1)
                await engine._drain_real_fill(provider, "EVID/SOL", T0 + 600)
        self.assertIn("EVID/SOL", engine.positions)
        self.assertEqual(broken.counters["errors"], 2)

    async def test_unfilled_entry_is_dropped(self):
        rec = ev.EvidenceRecorder(ev.EvidenceWriter(self.store))
        provider = PumpfunLivePaperProvider(watch_mints="")
        provider.register_watch_mint(MINT, base="EVID", reserves=INIT, source="logs")
        snap = provider.get_pumpfun_snapshot("EVID/SOL")
        sig = SignalOut(side="long", strength=0.8, reason="pump_paper_v1_entry", tags=[])
        rec.on_signal(symbol="EVID/SOL", mint=MINT, side="long", now_ms=T0, signal=sig, snapshot=snap,
                      notional=0.03, entry_impact_bps=None, params={}, provider=provider)
        self.assertEqual(rec.active(), 1)
        rec.poll(provider, T0 + ev.PENDING_DROP_MS + 1)
        self.assertEqual(rec.active(), 0)
        self.assertEqual(rec.counters["dropped_no_fill"], 1)
        self.assertFalse(self.store.exists())

    def test_episode_cap(self):
        rec = ev.EvidenceRecorder(ev.EvidenceWriter(self.store))
        provider = PumpfunLivePaperProvider(watch_mints="")
        sig = SignalOut(side="long", strength=0.8, reason="x", tags=[])
        for i in range(ev.MAX_EPISODES + 5):
            rec.on_signal(symbol=f"S{i}/SOL", mint=f"m{i}", side="long", now_ms=T0 + i, signal=sig, snapshot=None,
                          notional=0.03, entry_impact_bps=None, params={}, provider=provider)
        self.assertEqual(rec.active(), ev.MAX_EPISODES)
        self.assertEqual(rec.counters["dropped_cap"], 5)


class RotationTests(_Base):
    def test_rotates_and_keeps_bounded_files(self):
        os.environ["AUU_EVIDENCE_MAX_MB"] = "1"
        os.environ["AUU_EVIDENCE_KEEP"] = "2"
        w = ev.EvidenceWriter(self.store)
        blob = "x" * 300_000
        for i in range(12):
            w.write({"kind": "paper_trade_evidence", "i": i, "pad": blob})
        files = ev.evidence_files(self.store)
        self.assertEqual([p.name for p in files], ["paper_evidence.jsonl.2", "paper_evidence.jsonl.1", "paper_evidence.jsonl"])
        for p in files:
            self.assertLessEqual(p.stat().st_size, 1024 * 1024)
        seen = [r["i"] for r in replay.iter_records(files)]
        self.assertEqual(seen, sorted(seen), "oldest file first, in order")
        self.assertEqual(seen[-1], 11)
        self.assertGreater(seen[0], 0, "oldest records rotated away")


class ProviderFieldTests(unittest.TestCase):
    def test_trader_and_curve_evidence(self):
        provider = PumpfunLivePaperProvider(watch_mints="", clock=lambda: T0)
        provider.register_watch_mint(MINT, base="EVID", reserves=INIT, source="logs", creator=CREATOR)
        row = _print(provider, T0, 1.2, trader="walletZ")
        self.assertEqual(row["trader"], "walletZ")
        info = provider.curve_evidence("EVID/SOL")
        self.assertEqual((info["source"], info["creator"], info["created_ts"]), ("logs", CREATOR, T0))
        self.assertEqual(provider.curve_evidence("NOPE/SOL"), {})
        portal = provider.observe_portal_message(
            {"mint": MINT, "pool": "pump", "txType": "buy", "vSolInBondingCurve": 31, "vTokensInBondingCurve": 1.0e9,
             "solAmount": 0.1, "tokenAmount": 1000, "signature": "p1", "traderPublicKey": "walletP"}, recv_ts=T0 + 5)
        self.assertEqual(portal["trader"], "walletP")


def _record(prices_ms, *, entry_ms=0, exit_signal_ms=None, notional=0.03, fee_bps=125):
    """Evidence record from (dt_ms, price multiple) prints; entry filled on the print at ``entry_ms``."""
    rows = []
    for dt, mult in prices_ms:
        vs, vt = _vs_for_price(mult)
        rows.append([dt, 1, 0.1, vs, vt])
    e_row = next(r for r in rows if r[0] == entry_ms)
    px, qty, fee, _ = quote_curve_fill(side="buy", notional_sol=notional, virtual_sol_reserves=e_row[3],
                                       virtual_token_reserves=e_row[4], real_token_reserves=max(1, e_row[4] - (VT0 - INITIAL_REAL_TOKEN_RESERVES)),
                                       protocol_fee_bps=fee_bps, creator_fee_bps=0, mid=price_sol(e_row[3], e_row[4]))
    return {
        "kind": "paper_trade_evidence", "symbol": "R/SOL", "mint": "RMint",
        "entry": {"signal_ts": T0 - 300, "fill_ts": T0 + entry_ms, "fill_price": px, "fill_qty": qty, "fill_fee": fee, "notional_sol": notional},
        "exit": {"signal_ts": T0 + (exit_signal_ms if exit_signal_ms is not None else entry_ms)},
        "result": {"net_bps": None},
        "tape": {"t0": T0, "rows": rows},
    }


class ReplayTests(unittest.TestCase):
    # Entry at 1.0, rises to +8 % at 2 s, gaps to −30 % at 3.5 s.
    PATH = [(0, 1.0), (500, 1.01), (1_500, 1.03), (2_000, 1.08), (2_400, 1.07), (3_500, 0.70), (4_000, 0.69), (200_000, 0.5)]

    def test_take_profit_on_tick_grid(self):
        rec = _record(self.PATH)
        out = replay.simulate(rec, stop=0.05, tp=0.06, exit_delay_ms=300, tick_ms=1000)
        self.assertEqual(out["reason"], "take_profit")
        self.assertEqual(out["exit_signal_ts"], T0 + 2_000)
        self.assertEqual(out["exit_fill_ts"], T0 + 2_400)
        self.assertGreater(out["net_bps"], 0)

    def test_wider_tp_turns_into_gapped_stop(self):
        out = replay.simulate(_record(self.PATH), stop=0.05, tp=0.10, exit_delay_ms=300, tick_ms=1000)
        self.assertEqual(out["reason"], "stop_loss")
        self.assertEqual(out["exit_signal_ts"], T0 + 4_000)
        self.assertLess(out["net_bps"], -2_500, "fills at the gapped price, far below −5 %")
        self.assertLess(out["fill_vs_trigger_bps"], 0)

    def test_extra_delay_moves_the_fill(self):
        fast = replay.simulate(_record(self.PATH), stop=0.05, tp=0.06, exit_delay_ms=300, tick_ms=1000)
        slow = replay.simulate(_record(self.PATH), stop=0.05, tp=0.06, exit_delay_ms=1_500, tick_ms=1000)
        self.assertEqual(slow["exit_fill_ts"], T0 + 3_500)
        self.assertLess(slow["net_bps"], fast["net_bps"])

    def test_every_print_mode_and_max_hold_and_tape_end(self):
        per_print = replay.simulate(_record(self.PATH), stop=0.05, tp=0.02, exit_delay_ms=0, tick_ms=0)
        self.assertEqual(per_print["exit_signal_ts"], T0 + 1_500)
        flat = [(0, 1.0), (1_000, 1.001), (2_000, 1.002), (3_000, 1.001)]
        held = replay.simulate(_record(flat), stop=0.5, tp=0.5, max_hold_s=2, tick_ms=1000, exit_delay_ms=300)
        self.assertEqual(held["reason"], "max_hold")
        self.assertEqual(held["exit_fill_ts"], T0 + 3_000)
        end = replay.simulate(_record(flat), stop=0.5, tp=0.5, max_hold_s=999, tick_ms=1000)
        self.assertEqual((end["reason"], end["flag"]), ("tape_end", "tape_end"))

    def test_entry_delay_refills_later_and_costs_the_notional(self):
        base = replay.simulate(_record(self.PATH), stop=0.05, tp=0.06)
        late = replay.simulate(_record(self.PATH), stop=0.05, tp=0.06, entry_delay_ms=1_600)
        self.assertEqual(late["entry_ts"], T0 + 1_500)
        self.assertAlmostEqual(late["cost_sol"], 0.03, places=9)
        self.assertLess(late["net_bps"], base["net_bps"])

    def test_empty_tape_is_skipped(self):
        rec = _record(self.PATH)
        rec["tape"]["rows"] = []
        self.assertIsNone(replay.simulate(rec, stop=0.05, tp=0.06))

    def test_cli_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "e.jsonl"
            path.write_text(json.dumps(_record(self.PATH)) + "\n\nnot json\n", encoding="utf-8")
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = replay.main(["--evidence", str(path), "--tp", "0.10", "--json", "--per-trade"])
            self.assertEqual(code, 0)
            data = json.loads(buf.getvalue())
            self.assertEqual(data["summary"]["n_replayed"], 1)
            self.assertEqual(data["summary"]["by_reason"]["stop_loss"]["n"], 1)
            self.assertEqual(data["trades"][0]["reason"], "stop_loss")
            buf = io.StringIO()
            with redirect_stdout(buf):
                replay.main(["--evidence", str(path), "--per-trade"])
            self.assertIn("take_profit", buf.getvalue())


class ReplayReproducesEngineTests(_Base):
    _run_trade = EngineEvidenceTests._run_trade

    async def test_replay_matches_recorded_trade(self):
        await self._run_trade()
        rec = self._records()[0]
        out = replay.simulate(rec, stop=0.05, tp=0.06, exit_delay_ms=300, tick_ms=1000, fee_bps=125)
        self.assertEqual(out["reason"], "take_profit")
        self.assertEqual(out["exit_fill_ts"], rec["exit"]["fill_ts"])
        self.assertAlmostEqual(out["net_sol"], rec["result"]["net_sol"], places=6)


if __name__ == "__main__":
    unittest.main()
