"""Execution-price shadow record: book VWAP vs assumed slippage, separate table, never touches fills."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.backtest.costs import CostModel
from app.paper import exec_shadow as es
from app.paper import strategy_runner as sr
from tests.test_strategy_risk import Clock, _Base

BOOK = {"bids": [[99.9, 5], [99.8, 10], [99.0, 100]], "asks": [[100.1, 5], [100.2, 10], [101.0, 100]]}


class MeasureTests(unittest.TestCase):
    def test_vwap_walks_levels(self):
        vwap, filled, used = es.walk_book(BOOK["asks"], 1000.0)  # 500.5 at 100.1, then 499.5 at 100.2
        self.assertEqual(used, 2)
        self.assertAlmostEqual(filled, 1000.0)
        q = 5 + 499.5 / 100.2
        self.assertAlmostEqual(vwap, 1000.0 / q)

    def test_buy_and_sell_shortfall_and_deviation(self):
        b = es.measure(BOOK, "buy", 400.0, 100.05, 0.0002)
        self.assertAlmostEqual(b["mid"], 100.0)
        self.assertAlmostEqual(b["spread_bp"], 20.0)
        self.assertAlmostEqual(b["shortfall_mid_bp"], 10.0)
        self.assertAlmostEqual(b["deviation_bp"], 8.0)
        self.assertAlmostEqual(b["shortfall_close_bp"], (100.1 / 100.05 - 1) * 1e4)
        s = es.measure(BOOK, "sell", 400.0, 100.0, 0.0002)
        self.assertAlmostEqual(s["shortfall_mid_bp"], 10.0)  # positive = cost for both sides
        self.assertEqual(s["status"], "ok")

    def test_thin_book_is_partial_and_empty_book_errors(self):
        self.assertEqual(es.measure(BOOK, "buy", 1e9, 100.0, 0.0002)["status"], "partial")
        self.assertEqual(es.measure({"bids": [], "asks": []}, "buy", 10.0, 100.0, 0.0002)["status"], "error")

    def test_contract_size_conversion(self):
        b = es.to_base({"bids": [[100.0, 2]], "asks": [[101.0, 3]]}, 0.01)
        self.assertEqual(b["asks"], [[101.0, 0.03]])


class RecordTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.led = es.ExecShadowLedger(Path(self.tmp.name) / "exec_shadow.sqlite")

    def tearDown(self):
        self.led.close()
        self.tmp.cleanup()

    def test_record_skips_catchup_and_isolates_errors(self):
        fills = [{"coin": "BTC", "side": "buy", "notional": 400.0, "price": 100.0},
                 {"coin": "DOGE", "side": "sell", "notional": 50.0, "price": 100.0}]

        def book(c):
            if c == "DOGE":
                raise TimeoutError("okx")
            return "okx", f"{c}/USDT:USDT", BOOK

        es.record(self.led, 1, fills, catchup=False, book_fn=book)
        es.record(self.led, 2, fills, catchup=True, book_fn=book)
        rows = {(r["day"], r["coin"]): r for r in self.led.rows()}
        self.assertEqual(rows[(1, "BTC")]["status"], "ok")
        self.assertAlmostEqual(rows[(1, "BTC")]["assumed_slip"], CostModel().slippage["BTC"])
        self.assertEqual(rows[(1, "DOGE")]["status"], "error")
        self.assertEqual(rows[(2, "BTC")]["status"], "skipped_catchup")
        s = es.summarize(self.led.rows())
        self.assertEqual((s["n"], s["errors"], s["skipped"]), (1, 1, 2))
        self.assertAlmostEqual(s["coins"][0]["deviationBp"], 10.0 - 1.0)
        tables = {r[0] for r in self.led._db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertEqual(tables, {"quotes"})


class RunnerHookTests(_Base):
    def _run(self, hook):
        clock = Clock(0)
        self.at(clock, 150, 0)
        led = sr.StrategyLedger(Path(self.tmp.name) / f"s{id(hook)}.sqlite", now_ms=clock)
        self.ledgers.append(led)
        r = sr.StrategyRunner(led, panel_fn=self.panel_fn, ready_fn=lambda d: (True, ""), start_nav=10_000.0, now_ms=clock, on_commit=hook)
        for i in range(150, 171):
            self.at(clock, i, 0)
            r.tick()
        return [(x["day"], x["ret"], x["nav_close"], x["weights"]) for x in led.runs()], [dict(f) for f in led.fills(1000)]

    def test_hook_sees_fills_and_ledger_is_identical_even_if_it_fails(self):
        seen = []
        base = self._run(None)
        got = self._run(lambda d, f, c: seen.append((d, len(f), c)))

        def boom(d, f, c):
            raise RuntimeError("book api down")

        bad = self._run(boom)
        self.assertEqual(base, got)
        self.assertEqual(base, bad)
        self.assertEqual(sum(n for _, n, _ in seen), len(base[1]))
        self.assertTrue(all(not c for _, _, c in seen))


if __name__ == "__main__":
    unittest.main()
